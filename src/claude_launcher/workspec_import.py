"""Work spec executor: carries out :func:`workspec.plan`'s actions.

:mod:`claude_launcher.workspec` decides *what* an import does and has no side
effects. This module does it: board writes through ``claunch beads``, the mesh
and the sessions through the daemon API. Every write goes through a
:class:`Backend`, so the order and the idempotence are tested against a fake
one (``tests/test_workspec_import.py``) and the real one stays a thin shim.

A plan action is thin (keys and ids); the text an issue or a session receives
is composed here from the spec document, so the plan stays readable as a
dry run and the wording lives in one place.

Rerunning an import continues it. Every created thing has a fixed identity —
an issue its external ref ``spec:<name>:<key>``, the mesh its name, an agent
its handle in that mesh — and what already exists is reused, not recreated.
Dependencies and decision comments are checked against the board before
they are written, so a rerun after a stop anywhere in the board phase writes
the missing ones once and duplicates none.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import workspec

#: Result words of one executed action.
CREATED = "created"
EXISTS = "exists"
DONE = "done"
SKIPPED = "skipped"


class SpecImportError(Exception):
    """An import stopped. Actions before it are kept (a rerun continues)."""


class Backend:
    """The writes an import performs. :class:`DaemonBackend` is the real one."""

    # board
    def find_issues(self, label: str) -> Dict[str, dict]:
        """external_ref -> issue (``id``, ``assignee``) for every issue with
        ``label``, closed ones included."""
        raise NotImplementedError

    def create_issue(self, *, title: str, type: str, priority: int,
                     labels: List[str], description: str,
                     external_ref: str, parent: Optional[str]) -> str:
        raise NotImplementedError

    def add_dep(self, issue: str, blocked_by: str) -> None:
        raise NotImplementedError

    def comment(self, issue: str, text: str) -> None:
        raise NotImplementedError

    def blockers(self, issue: str) -> set:
        """Ids ``issue`` already depends on with a ``blocks`` edge."""
        raise NotImplementedError

    def comments(self, issue: str) -> List[str]:
        raise NotImplementedError

    def assign(self, issue: str, session: str) -> None:
        raise NotImplementedError

    # mesh and sessions
    def get_mesh(self, mesh: str) -> Optional[dict]:
        """The mesh's ``members``/``member_links``/``roles`` view, or None."""
        raise NotImplementedError

    def create_mesh(self, mesh: str) -> None:
        raise NotImplementedError

    def get_roles(self, mesh: str) -> dict:
        """``{"custom": bool, "yaml": str}`` — the mesh's role document."""
        raise NotImplementedError

    def put_roles(self, mesh: str, doc: dict) -> None:
        raise NotImplementedError

    def join(self, mesh: str, session: str, handle: str, role: str) -> None:
        raise NotImplementedError

    def spawn(self, parent: Optional[str], body: dict) -> str:
        """Start a session (a top-level one when ``parent`` is None) and
        return its name."""
        raise NotImplementedError

    def connect(self, mesh: str, a: str, b: str) -> None:
        raise NotImplementedError

    def file_exists(self, path: str) -> bool:
        return Path(path).is_file()


class DaemonBackend(Backend):
    """Board through the ``claunch beads`` CLI (no daemon API creates a
    dependency), everything else through the daemon API."""

    def __init__(self, client, board_cwd: str) -> None:
        self.client = client
        self.board_cwd = board_cwd

    def _beads(self, args: List[str]):
        proc = subprocess.run(
            [sys.executable, "-m", "claude_launcher.cli", "beads", *args,
             "--json"],
            cwd=self.board_cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if proc.returncode != 0:
            raise SpecImportError(
                f"claunch beads {args[0]} failed: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        try:
            return json.loads(proc.stdout) if proc.stdout.strip() else None
        except ValueError:
            return None

    def find_issues(self, label):
        data = self._beads(["list", "--all", "--limit", "0", "--label", label])
        rows = data if isinstance(data, list) else (data or {}).get("issues", [])
        return {r["external_ref"]: r for r in rows
                if isinstance(r, dict) and r.get("external_ref")}

    def create_issue(self, *, title, type, priority, labels, description,
                     external_ref, parent):
        args = ["create", title, "--type", type, "--priority", str(priority),
                "--labels", ",".join(labels), "--description", description,
                "--external-ref", external_ref]
        if parent:
            args += ["--parent", parent]
        data = self._beads(args)
        if isinstance(data, list) and data:
            data = data[0]
        iid = (data or {}).get("id") if isinstance(data, dict) else None
        if not iid:
            raise SpecImportError(f"br create answered without an id ({title})")
        return str(iid)

    def add_dep(self, issue, blocked_by):
        self._beads(["dep", "add", issue, blocked_by, "-t", "blocks"])

    def comment(self, issue, text):
        self._beads(["comments", "add", issue, text])

    def blockers(self, issue):
        rows = self._beads(["dep", "list", issue]) or []
        return {r["depends_on_id"] for r in rows
                if isinstance(r, dict) and r.get("type") == "blocks"}

    def comments(self, issue):
        rows = self._beads(["comments", issue]) or []
        return [str(r.get("text") or "") for r in rows if isinstance(r, dict)]

    def assign(self, issue, session):
        self._beads(["update", issue, "--assignee", session])

    def get_mesh(self, mesh):
        from .daemon_client import DaemonClientError
        try:
            return self.client.get(f"/api/mesh/{mesh}")
        except DaemonClientError as exc:
            if "no mesh named" in str(exc):
                return None
            raise

    def create_mesh(self, mesh):
        self.client.post("/api/mesh", {"name": mesh})

    def get_roles(self, mesh):
        return self.client.get(f"/api/mesh/{mesh}/roles")

    def put_roles(self, mesh, doc):
        self.client.put(f"/api/mesh/{mesh}/roles", {"roles": doc})

    def join(self, mesh, session, handle, role):
        self.client.post(f"/api/mesh/{mesh}/members",
                         {"session": session, "handle": handle, "role": role})

    def spawn(self, parent, body):
        if parent is None:
            out = self.client.post("/api/sessions", body)
            return str(out.get("name") or (out.get("session") or {}).get("name"))
        out = self.client.post(f"/api/sessions/{parent}/children", body)
        if out.get("pending"):
            # An agent's spawn waits for the person (claunch-iw5i8.16); an
            # import needs each child's name for the next step, so it cannot
            # run from a managed session's shell.
            raise SpecImportError(
                f"the spawn of a child of {parent} is waiting for approval "
                f"({(out.get('request') or {}).get('id')}): run the import from "
                "your own shell, not from a managed session")
        return str((out.get("session") or {}).get("name"))

    def connect(self, mesh, a, b):
        self.client.patch(f"/api/mesh/{mesh}/members/{a}/links/{b}",
                          {"enabled": True})


# --------------------------------------------------------------------------- #
# text composed from the document
# --------------------------------------------------------------------------- #

def _lines(value) -> str:
    if isinstance(value, list):
        return "\n".join(f"- {v}" for v in value)
    return str(value or "").strip()


def _ref_line(ref: dict) -> str:
    kind = ref.get("kind")
    if kind == "text":
        return f"- {ref['id']}:\n" + "\n".join(
            f"  {ln}" for ln in str(ref["text"]).strip().splitlines())
    return f"- {ref['id']} ({kind}): {ref.get(kind)}"


def issue_body(doc: dict, it: dict, ids: Dict[str, str]) -> str:
    """An issue's description: the board's headings, then what the spec
    attaches to it (references, contracts, gds6 fields)."""
    refs = {r["id"]: r for r in workspec._list(doc, "references")}
    out = [f"## 목표\n{_lines(it.get('goal')) or it['title']}"]
    if it.get("scope"):
        out.append(f"## 범위\n{_lines(it['scope'])}")
    done = it.get("done_when") or (it.get("contract") or {}).get("done_when")
    if done:
        out.append(f"## 완료 증거 기준\n{_lines(done)}")
    # The epic carries every reference; an issue the ones it names.
    names = list(refs) if it.get("type") == "epic" else it.get("refs") or []
    if names:
        out.append("## 참고 자료\n" + "\n".join(_ref_line(refs[n])
                                                for n in names))
    if it.get("contracts"):
        out.append("## 계약\n" + "\n".join(
            f"- {c}: {ids.get('contract:' + c, '?')}" for c in it["contracts"]))
    extra = {k: it[k] for k in ("rule_ids", "size", "changes", "contract")
             if it.get(k) is not None}
    if extra:
        out.append("## 명세 필드\n```yaml\n"
                   + yaml.safe_dump(extra, allow_unicode=True,
                                    sort_keys=False).strip() + "\n```")
    if it.get("assignee") == "human" and doc.get("human_steps"):
        out.append("## 사람 몫\n" + "\n".join(
            f"- {s.get('file')}: {s.get('what')} ({s.get('why')})"
            if isinstance(s, dict) else f"- {s}"
            for s in doc["human_steps"]))
    gates = [d["key"] for d in workspec._list(doc, "decisions")
             if it["key"] in (d.get("gates") or [])]
    if gates:
        out.append("## 미결 결정\n" + "\n".join(
            f"- {d['key']}: {d['question']} (owner {d.get('owner')}, "
            f"recommended {d.get('recommended')})"
            for d in workspec._list(doc, "decisions") if d["key"] in gates))
    out.append(f"## 출처\nwork spec {doc['name']} · key {it['key']}"
               + (f" · {doc.get('title')}" if doc.get("title") else ""))
    return "\n\n".join(out)


def contract_body(doc: dict, c: dict) -> str:
    refs = {r["id"]: r for r in workspec._list(doc, "references")}
    out = [f"## 목표\n{str(c['text']).strip()}"]
    if c.get("parties"):
        out.append("## 당사자\n" + _lines(c["parties"]))
    if c.get("refs"):
        out.append("## 참고 자료\n" + "\n".join(_ref_line(refs[r])
                                                for r in c["refs"]))
    out.append(f"## 출처\nwork spec {doc['name']} · contract {c['id']} "
               f"({c.get('kind')})")
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# the executor
# --------------------------------------------------------------------------- #

class Importer:
    """Runs a plan against a :class:`Backend` and records what happened."""

    def __init__(self, doc: dict, backend: Backend, *,
                 profile: str = "", base_dir: str = "") -> None:
        self.doc = doc
        self.b = backend
        self.profile = profile
        self.base_dir = base_dir or os.getcwd()
        self.tag = f"spec:{doc['name']}"
        self.ids: Dict[str, str] = {}       # spec key -> board id
        self.sessions: Dict[str, str] = {}  # agent id -> session name
        self.log: List[dict] = []
        self.agents = {a["id"]: a for a in workspec._list(doc, "agents")}
        self.issues = {i["key"]: i for i in workspec._list(doc, "issues")}
        self._existing: Optional[Dict[str, dict]] = None
        self._members: Dict[str, Dict[str, dict]] = {}

    # -- helpers --------------------------------------------------------- #

    def _note(self, act: dict, result: str, detail: str = "") -> None:
        self.log.append({"do": act["do"], "what": _what(act),
                         "result": result, "detail": detail})

    def handle(self, agent: str) -> str:
        if agent == workspec.LEADER_ID:
            lead = (self.doc.get("leader") or {})
            return self._leader_handle or (lead.get("shared") or {}).get(
                "handle") or "leader"
        a = self.agents[agent]
        return str(a.get("handle") or a["id"])

    _leader_handle: str = ""

    def _member_map(self, mesh: str, refresh: bool = False) -> Dict[str, dict]:
        if refresh or mesh not in self._members:
            view = self.b.get_mesh(mesh) or {}
            self._members[mesh] = {
                m["handle"]: m for m in view.get("members") or []
                if m.get("category") != "killed"
            }
            self._members[mesh]["__links__"] = {
                (lk["a"], lk["b"]) for lk in view.get("member_links") or []
                if lk.get("enabled")
            }
        return self._members[mesh]

    # -- actions --------------------------------------------------------- #

    def run(self, actions: List[dict]) -> List[dict]:
        for act in actions:
            getattr(self, "_do_" + act["do"].replace("-", "_"))(act)
        return self.log

    def _do_leader_check(self, act):
        members = self._member_map(act["mesh"], refresh=True)
        if not self.b.get_mesh(act["mesh"]):
            raise SpecImportError(f"mesh {act['mesh']!r} does not exist")
        leaders = [m for h, m in members.items() if h != "__links__"
                   and "leader" in (m.get("roles") or [m.get("role")])]
        if not leaders:
            raise SpecImportError(f"mesh {act['mesh']!r} has no live leader")
        lead = leaders[0]
        if act.get("session") and lead.get("session") != act["session"]:
            raise SpecImportError(
                f"{act['session']!r} is not the leader of {act['mesh']!r} "
                f"(the leader is {lead.get('session')!r})")
        self.sessions[workspec.LEADER_ID] = lead["session"]
        self._leader_handle = lead["handle"]
        self._note(act, DONE, f"leader {lead['session']} ({lead['handle']})")

    def _do_reference(self, act):
        ref = next(r for r in workspec._list(self.doc, "references")
                   if r["id"] == act["id"])
        if act["kind"] == "file":
            path = os.path.join(self.base_dir, ref["file"])
            if not self.b.file_exists(path):
                raise SpecImportError(f"reference {act['id']}: no file {path}")
            self._note(act, DONE, path)
        else:
            self._note(act, SKIPPED, "carried in issue bodies")

    def _existing_issues(self) -> Dict[str, dict]:
        if self._existing is None:
            self._existing = self.b.find_issues(self.tag)
        return self._existing

    def _do_issue(self, act):
        found = self._existing_issues().get(act["external_ref"])
        if found:
            self.ids[act["key"]] = str(found["id"])
            self._note(act, EXISTS, str(found["id"]))
            return
        if act["key"].startswith("contract:"):
            c = next(c for c in workspec._list(self.doc, "contracts")
                     if c["id"] == act["key"].split(":", 1)[1])
            title = f"[계약] {c.get('title') or c['id']}"
            body = contract_body(self.doc, c)
            prio, labels, parent = 3, [self.tag, "contract"], None
        else:
            it = self.issues[act["key"]]
            title, body = it["title"], issue_body(self.doc, it, self.ids)
            prio, labels = act["priority"], act["labels"]
            parent = self.ids.get(act["parent"]) if act.get("parent") else None
        iid = self.b.create_issue(
            title=title, type=act["type"], priority=prio, labels=labels,
            description=body, external_ref=act["external_ref"], parent=parent)
        self.ids[act["key"]] = iid
        self._note(act, CREATED, iid)

    def _do_dep(self, act):
        iid, dep = self.ids[act["issue"]], self.ids[act["blocked_by"]]
        if dep in self.b.blockers(iid):
            self._note(act, EXISTS)
            return
        self.b.add_dep(iid, dep)
        self._note(act, DONE)

    def _do_comment(self, act):
        iid = self.ids[act["issue"]]
        if act["text"] in self.b.comments(iid):
            self._note(act, EXISTS)
            return
        self.b.comment(iid, act["text"])
        self._note(act, DONE)

    def _do_mesh_create(self, act):
        if self.b.get_mesh(act["mesh"]) is not None:
            self._note(act, EXISTS)
            return
        self.b.create_mesh(act["mesh"])
        self._note(act, CREATED)

    def _do_mesh_roles(self, act):
        current = self.b.get_roles(act["mesh"])
        base = (yaml.safe_load(current.get("yaml") or "") or {}) \
            if current.get("custom") else {}
        spec_roles = self.doc.get("roles") or {}
        roles = dict(base.get("roles") or {})
        roles.update(spec_roles.get("roles") or {})
        for name in act["subroles"]:
            prompt = str(self.doc["subroles"][name]["prompt"]).strip()
            roles[name] = {"stance": prompt}
        doc = {**base, **{k: v for k, v in spec_roles.items() if k != "roles"},
               "roles": roles}
        if base and doc == base:
            self._note(act, EXISTS)
            return
        self.b.put_roles(act["mesh"], doc)
        self._note(act, DONE, ", ".join(sorted(roles)))

    def _do_mesh_join(self, act):
        members = self._member_map(act["mesh"], refresh=True)
        self._leader_handle = act["handle"]
        self.sessions[workspec.LEADER_ID] = act["session"]
        held = members.get(act["handle"])
        if held:
            if held.get("session") != act["session"]:
                raise SpecImportError(
                    f"handle {act['handle']!r} in {act['mesh']!r} is held by "
                    f"{held.get('session')!r}")
            self._note(act, EXISTS)
            return
        self.b.join(act["mesh"], act["session"], act["handle"], act["role"])
        self._note(act, CREATED, act["session"])

    def _do_spawn(self, act):
        members = self._member_map(act["mesh"])
        if act["agent"] == workspec.LEADER_ID:
            self._leader_handle = "leader"
        handle = self.handle(act["agent"])
        held = members.get(handle)
        if held:
            self.sessions[act["agent"]] = held["session"]
            self._note(act, EXISTS, held["session"])
            return
        body = self._spawn_body(act, handle)
        if act["parent"] is None:
            if not self.profile:
                raise SpecImportError(
                    "an own leader is a new top-level session and needs a "
                    "profile: pass --profile")
            body["profile"] = self.profile
            parent = None
        else:
            parent = self.sessions[act["parent"]]
        name = self.b.spawn(parent, body)
        self.sessions[act["agent"]] = name
        members[handle] = {"handle": handle, "session": name}
        self._note(act, CREATED, name)

    def _spawn_body(self, act: dict, handle: str) -> dict:
        body = {"mesh": act["mesh"], "handle": handle, "role": act["role"],
                "task": self.task(act)}
        # An empty workflow is "none": omitting it would start the parent's
        # paired child workflow instead (a reviewer answers asks, no run).
        body["workflow"] = act.get("workflow") or "-"
        if self.doc.get("workspace"):
            body["workspace"] = self.doc["workspace"]
        if act["agent"] == workspec.LEADER_ID:
            own = (self.doc.get("leader") or {}).get("own") or {}
            if own.get("name"):
                body["name"] = own["name"]
            body["beads"] = False
            return body
        a = self.agents[act["agent"]]
        if act.get("subroles"):
            body["subroles"] = act["subroles"]
        for key in ("profile", "model", "worktree"):
            if a.get(key):
                body[key] = a[key]
        if "worktree" not in body and act["kind"] != "reviewer":
            # Two code-editing children in one checkout overwrite each other.
            body["worktree"] = f"spec-{self.doc['name']}-{a['id']}"
        if act.get("issue"):
            body["issue"] = self.ids[act["issue"]]
        else:
            # The API's spelling. ``no_issue`` is the MCP tool's, renamed to
            # this before it reaches the daemon (mesh_mcp._SPAWN_RENAMED);
            # sent raw it is ignored and the daemon mints a placeholder.
            body["beads"] = False
        return body

    def task(self, act: dict) -> str:
        """The opening task: the assignment. A subrole's prompt is not
        repeated here: it is that role's stance, which the join briefing
        hands over with its text id (``MeshManager._stance_lines``,
        claunch-bwukr)."""
        doc, mesh = self.doc, act["mesh"]
        parts: List[str] = []
        head = (f"work spec {doc['name']}"
                + (f" — {doc['title']}" if doc.get("title") else "")
                + f". 메시 {mesh}.")
        if act["agent"] == workspec.LEADER_ID:
            epics = [self.ids[k] for k, i in self.issues.items()
                     if i.get("type") == "epic" and k in self.ids]
            parts.append(
                f"{head} 너는 이 메시의 리더다. 에이전트와 이슈는 import 가 "
                "만든다. 착지 요청을 받아 통합한다."
                + (f" epic: {', '.join(epics)}." if epics else ""))
            return "\n\n".join(parts)
        a = self.agents[act["agent"]]
        mine = [k for k, i in self.issues.items()
                if i.get("assignee") == a["id"]]
        lines = [f"{head} 너는 에이전트 {a['id']}({act['kind']})다. "
                 f"부모: {self.handle(act['parent'])}."]
        if mine:
            lines.append("큐(우선순위 순서): " + ", ".join(
                f"{self.ids.get(k, k)} {self.issues[k]['title']}"
                for k in sorted(mine,
                                key=lambda k: self.issues[k].get("priority", 2))))
        reviews = sorted({self.issues[k]["review"] for k in mine
                          if self.issues[k].get("review")})
        if reviews:
            lines.append("리뷰어: " + ", ".join(self.handle(r) for r in reviews))
        if act["kind"] == "reviewer":
            mine_r = [k for k, i in self.issues.items() if i.get("review") == a["id"]]
            lines.append("리뷰 대상: " + (", ".join(
                f"{self.ids.get(k, k)} ({self.handle(self.issues[k]['assignee'])})"
                for k in mine_r) or "없음") + ".")
        if act["kind"] == "sub-lead":
            kids = [self.handle(x["id"]) for x in self.agents.values()
                    if x.get("parent") == a["id"]]
            lines.append(
                "너는 2nd leader 다. 첫 회차에 stack 서브 런을 연다(cflow "
                "start workflow: stack, sub: stack). 자식"
                + (f"({', '.join(kids)})" if kids else "")
                + "의 착지 요청은 네가 받아 <너>-stack 브랜치로 모으고, 리더에게는 "
                "그 브랜치 하나를 착지 요청한다. 자식에게 기준 브랜치를 "
                "<너>-stack 으로 알린다.")
        if a.get("task"):
            lines.append(str(a["task"]).strip())
        parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def _do_assign(self, act):
        iid, session = self.ids[act["issue"]], self.sessions[act["agent"]]
        found = self._existing_issues().get(f"{self.tag}:{act['issue']}")
        if found and found.get("assignee") == session:
            self._note(act, EXISTS)
            return
        self.b.assign(iid, session)
        self._note(act, DONE, session)

    def _do_stack(self, act):
        self._note(act, SKIPPED, "the sub-lead's opening task opens it")

    def _do_connect(self, act):
        members = self._member_map(act["mesh"], refresh=True)
        a, b = self.handle(act["a"]), self.handle(act["b"])
        if (a, b) in members["__links__"] or (b, a) in members["__links__"]:
            self._note(act, EXISTS)
            return
        self.b.connect(act["mesh"], a, b)
        self._note(act, DONE)


def _what(act: dict) -> str:
    for key in ("key", "agent", "issue", "id", "mesh"):
        if act.get(key):
            return str(act[key])
    return ""


def board_dir(doc: dict, default: str = "") -> str:
    """Where the board lives and ``file`` references resolve: the spec's
    registered workspace, else ``default`` (the current directory)."""
    if doc.get("workspace"):
        from .spawn import SpawnDenied, resolve_workspace
        try:
            return resolve_workspace(str(doc["workspace"]))
        except SpawnDenied as exc:
            raise workspec.SpecError(str(exc)) from None
    return default or os.getcwd()


def describe(act: dict) -> str:
    """One dry-run line for ``claunch spec plan``."""
    do = act["do"]
    rest = {k: v for k, v in act.items() if k != "do" and v not in (None, [], "")}
    return f"{do:12} " + " ".join(f"{k}={v}" for k, v in rest.items())


def execute(doc: dict, backend: Backend, *, leader_choice: str = "",
            profile: str = "", base_dir: str = "") -> List[dict]:
    """Validate, plan and run. Raises :class:`workspec.SpecError` for an
    invalid spec (nothing written) and :class:`SpecImportError` when a step
    stops the run (what came before it stays, and a rerun continues)."""
    actions = workspec.plan(doc, leader_choice)
    imp = Importer(doc, backend, profile=profile, base_dir=base_dir)
    try:
        imp.run(actions)
    except SpecImportError as exc:
        exc.log = imp.log  # type: ignore[attr-defined]
        raise
    return imp.log
