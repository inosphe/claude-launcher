"""Durable, per-clone local synchronization policy and request ownership.

The database lives in git's common directory: worktree and workspace aliases
share it, while independent clones of the same remote do not. Transactions
serialize reservations and fence stale service generations. No transaction is
held while waiting for a person, a PR or a network fetch.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import time

from . import reposync


class PolicyError(reposync.SyncError):
    pass


def common_dir(repo: str) -> Path:
    return Path(os.path.realpath(reposync.git(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir")))


def policy(repo: str) -> dict:
    path = common_dir(repo) / "claunch-local-sync.sqlite3"
    if not path.exists():
        return {}
    with sqlite3.connect(str(path), timeout=10) as db:
        row = db.execute("SELECT value FROM policy WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {}


def remote_key(repo: str, remote: str) -> str:
    value = reposync.remotes(repo).get(remote)
    if not value:
        raise PolicyError("unknown remote")
    key = reposync.url_key(value)
    if key:
        return key
    if value.startswith("file://"):
        return "file://" + os.path.realpath(value[7:])
    if ":" not in value:
        return "file://" + os.path.realpath(os.path.join(repo, value))
    raise PolicyError("remote identity cannot be normalized; explicit mapping required")


def protected(repo: str, remote: str, branch: str) -> bool:
    p = policy(repo)
    if not p:
        return False
    ref = branch if branch.startswith("refs/heads/") else "refs/heads/" + branch
    # Local protection survives a remote rename/URL change. A changed identity
    # is a reason to stop, never a way to silently remove the policy.
    return any(t["ref"] == ref for t in p["targets"])


def require_pr(repo: str, remote: str, branch: str, operation: str) -> None:
    if protected(repo, remote, branch):
        raise PolicyError(f"PR-only target {branch}: {operation} refused; request local-sync-manager")


class Registry:
    def __init__(self, repo: str, daemon: str, *, boot: str = ""):
        if not daemon:
            raise PolicyError("daemon identity required")
        self.repo = repo
        self.daemon = daemon
        self.path = common_dir(repo) / "claunch-local-sync.sqlite3"
        self.key = hashlib.sha256((daemon + "\0" + str(self.path.parent)).encode()).hexdigest()
        with self.tx() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS checkout_consents (id TEXT PRIMARY KEY, binding TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS boots (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS managers
                  (key TEXT PRIMARY KEY, epoch INTEGER NOT NULL, session TEXT, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests
                  (id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT);
            ''')

        if boot:
            with self.tx() as db:
                previous = db.execute("SELECT value FROM boots WHERE key=?", (self.key,)).fetchone()
                if previous is not None and previous[0] != boot:
                    db.execute("UPDATE managers SET epoch=epoch+1,session=NULL,state='needs_operator' WHERE key=?", (self.key,))
                db.execute("INSERT OR REPLACE INTO boots VALUES (?,?)", (self.key, boot))

    @contextmanager
    def tx(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def configure(self, targets: list, *, expected_revision: int, profile: str = "") -> dict:
        """Called only by the person-only configuration surface.

        Authority is explicit per remote/ref; unknown cross-host authority is
        deliberately unresolved. This local registry does not elect a leader.
        """
        if not targets:
            raise PolicyError("at least one target required")
        seen = set()
        for target in targets:
            if set(target) != {"remote_key", "ref", "mesh", "authority", "authority_revision"}:
                raise PolicyError("target requires remote_key/ref/mesh/authority/authority_revision")
            if not all(isinstance(v, str) and v for v in target.values()):
                raise PolicyError("target binding fields must be nonempty strings")
            ref = target["ref"]
            if not ref.startswith("refs/heads/") or not reposync._ok(self.repo, "check-ref-format", ref):
                raise PolicyError("invalid target ref")
            if ref in seen:
                raise PolicyError("ambiguous authority binding for target")
            seen.add(ref)
        with self.tx() as db:
            row = db.execute("SELECT value FROM policy WHERE id=1").fetchone()
            old = json.loads(row[0]) if row else {}
            if old.get("revision", 0) != expected_revision:
                raise PolicyError("policy changed; refresh before configuring")
            value = {"revision": expected_revision + 1, "integration_mode": "pr_only",
                     "targets": targets, "provision_mode": "ask", "profile": profile}
            db.execute("INSERT OR REPLACE INTO policy VALUES (1,?)", (json.dumps(value),))
            db.execute("UPDATE managers SET epoch=epoch+1, state='needs_operator', session=NULL")
            db.execute("UPDATE requests SET state='needs_operator' WHERE state NOT IN ('complete','cancelled')")
        return value

    def submit(self, payload: dict) -> dict:
        fields = {"id", "requester", "mesh", "board", "issue", "remote", "ref",
                  "required_sha", "policy_revision", "authority_revision"}
        if set(payload) != fields:
            raise PolicyError("request fields must be " + ", ".join(sorted(fields)))
        if any(not isinstance(payload[k], str) or not payload[k] for k in fields - {"policy_revision"}):
            raise PolicyError("request identity and binding fields must be nonempty strings")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", payload["required_sha"]):
            raise PolicyError("required_sha must be a full commit SHA")
        with self.tx() as db:
            self._binding(db, payload)
            encoded = json.dumps(payload, sort_keys=True)
            old = db.execute("SELECT * FROM requests WHERE id=?", (payload["id"],)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise PolicyError("idempotency key reused for a different request")
                return dict(old)
            db.execute("INSERT INTO requests VALUES (?,?,'pending',NULL)", (payload["id"], encoded))
        return self.get(payload["id"])

    def _binding(self, db, payload):
        row = db.execute("SELECT value FROM policy WHERE id=1").fetchone()
        p = json.loads(row[0]) if row else {}
        if p.get("revision") != payload["policy_revision"]:
            raise PolicyError("stale or absent policy")
        key = remote_key(self.repo, payload["remote"])
        target = next((t for t in p["targets"] if t["ref"] == payload["ref"] and t["remote_key"] == key), None)
        if not target or target["authority_revision"] != payload["authority_revision"]:
            raise PolicyError("unknown or stale authority binding")
        # Requests from other meshes are retained as distinct contexts. The
        # authority is always this explicit binding, never the request's leader.
        return target

    def reserve(self) -> dict:
        """One durable creation reservation; absent provisioning consent waits."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM managers WHERE key=?", (self.key,)).fetchone()
            if not row:
                db.execute("INSERT INTO managers VALUES (?,1,NULL,'needs_operator')", (self.key,))
                row = db.execute("SELECT * FROM managers WHERE key=?", (self.key,)).fetchone()
            return dict(row)

    def bind(self, session: str, *, epoch: int, role: str, workflow: str) -> dict:
        if role != "local-sync-manager" or workflow != "local-sync-v2" or not session:
            raise PolicyError("an explicit local-sync-manager running local-sync-v2 is required")
        with self.tx() as db:
            cur = db.execute("UPDATE managers SET session=?,state='ready' WHERE key=? AND epoch=? AND state='needs_operator'",
                             (session, self.key, epoch))
            if cur.rowcount != 1:
                raise PolicyError("stale manager reservation")
        return self.reserve()

    def retire(self, *, epoch: int) -> None:
        with self.tx() as db:
            cur = db.execute("UPDATE managers SET epoch=epoch+1,session=NULL,state='needs_operator' WHERE key=? AND epoch=?",
                             (self.key, epoch))
            if cur.rowcount != 1:
                raise PolicyError("stale manager generation")

    def get(self, rid: str) -> dict:
        with self.tx() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise PolicyError("unknown request")
            return dict(row)

    def settle(self, rid: str, state: str) -> dict:
        if state not in ("held", "cancelled", "pending"):
            raise PolicyError("invalid operator resolution")
        with self.tx() as db:
            db.execute("UPDATE requests SET state=? WHERE id=? AND state!='complete'", (state, rid))
        return self.get(rid)

    def approve_checkout(self, rid: str, *, epoch: int, old: str, new: str) -> dict:
        """Person-only consent for the exact checked-out FF, never a reset."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if not row or row["state"] != "needs_operator" or not row["receipt"]:
                raise PolicyError("no checkout plan waiting for approval")
            plan = json.loads(row["receipt"])
            if not plan.get("checkout") or (plan["old"], plan["upstream"]) != (old, new):
                raise PolicyError("checkout plan changed")
            payload = json.loads(row["payload"])
            self._binding(db, payload)
            binding = {"old": old, "new": new, "epoch": epoch,
                       "revision": payload["policy_revision"], "expires": time.time() + 1800}
            db.execute("INSERT OR REPLACE INTO checkout_consents VALUES (?,?)", (rid, json.dumps(binding)))
            return binding

    def apply(self, rid: str, *, session: str, epoch: int) -> dict:
        row = self.get(rid)
        if row["state"] == "complete":
            return json.loads(row["receipt"])
        if row["state"] not in ("pending", "waiting_dependency", "needs_operator"):
            raise PolicyError("request is held or cancelled")
        payload = json.loads(row["payload"])
        # Fetch may block; do it outside the write transaction, then recheck all
        # authority/lease/ref facts immediately before the local CAS.
        reposync.fetch(self.repo, payload["remote"])
        with self.tx() as db:
            current = db.execute("SELECT state FROM requests WHERE id=?", (rid,)).fetchone()[0]
            if current not in ("pending", "waiting_dependency", "needs_operator"):
                raise PolicyError("request changed during fetch")
            self._binding(db, payload)
            manager = db.execute("SELECT * FROM managers WHERE key=?", (self.key,)).fetchone()
            if not manager or (manager["session"], manager["epoch"], manager["state"]) != (session, epoch, "ready"):
                raise PolicyError("stale manager lease")
            branch = payload["ref"][len("refs/heads/"):]
            upstream = reposync.git(self.repo, "rev-parse", "--verify", f"refs/remotes/{payload['remote']}/{branch}^{{commit}}")
            old = reposync.git(self.repo, "rev-parse", "--verify", payload["ref"])
            if not reposync._ok(self.repo, "merge-base", "--is-ancestor", payload["required_sha"], upstream):
                raise PolicyError("fetched target does not contain requested commit")
            if not reposync._ok(self.repo, "merge-base", "--is-ancestor", old, upstream):
                # Preserve first. No reset, rebase or push of the protected ref.
                reposync.git(self.repo, "update-ref", "refs/claunch/preserved/" + old, old)
                result = {"status": "needs_operator", "old": old, "upstream": upstream,
                          "preserved": "refs/claunch/preserved/" + old}
                db.execute("UPDATE requests SET state='needs_operator',receipt=? WHERE id=?", (json.dumps(result), rid))
                return result
            checkout = reposync.checkout_of(self.repo, branch)
            if old != upstream and checkout:
                consent = db.execute("SELECT binding FROM checkout_consents WHERE id=?", (rid,)).fetchone()
                consent = json.loads(consent[0]) if consent else {}
                expected = (old, upstream, epoch, payload["policy_revision"])
                actual = tuple(consent.get(k) for k in ("old", "new", "epoch", "revision"))
                if actual != expected or consent.get("expires", 0) <= time.time():
                    plan = {"status": "needs_operator", "checkout": checkout, "old": old, "upstream": upstream}
                    db.execute("UPDATE requests SET state='needs_operator',receipt=? WHERE id=?", (json.dumps(plan), rid))
                    return plan
                if (reposync.git(checkout, "status", "--porcelain")
                        or reposync.git(checkout, "symbolic-ref", "HEAD") != payload["ref"]
                        or reposync.git(checkout, "rev-parse", "HEAD") != old):
                    raise PolicyError("checkout changed or dirty; obtain owner coordination again")
                reposync.git(checkout, "merge", "--ff-only", "--no-edit", upstream)
                db.execute("DELETE FROM checkout_consents WHERE id=?", (rid,))
            elif old != upstream:
                reposync.git(self.repo, "update-ref", payload["ref"], upstream, old)
            if reposync.git(self.repo, "rev-parse", payload["ref"]) != upstream:
                raise PolicyError("local target changed during synchronization")
            receipt = {"status": "complete", "request_id": rid, "daemon": self.daemon,
                       "repo": str(self.path.parent), "old": old, "new": upstream,
                       "required_sha": payload["required_sha"], "epoch": epoch,
                       "policy_revision": payload["policy_revision"], "time": time.time()}
            db.execute("UPDATE requests SET state='complete',receipt=? WHERE id=?", (json.dumps(receipt), rid))
            return receipt


def allow_local(repo: str) -> None:
    """Fail closed for legacy local-landing workflows in a managed repository."""
    if policy(repo):
        raise PolicyError("PR-only repository: local landing is disabled; restore the PR path")


def verify_receipt(repo: str, rid: str, epoch: int) -> None:
    path = common_dir(repo) / "claunch-local-sync.sqlite3"
    if not path.exists():
        raise PolicyError("no durable synchronization receipt")
    with sqlite3.connect(str(path), timeout=10) as db:
        row = db.execute("SELECT state,receipt FROM requests WHERE id=?", (rid,)).fetchone()
    if row is None or row[0] not in ("complete", "needs_operator", "held", "cancelled"):
        raise PolicyError("request has no terminal or operator-held result")
    if row[0] in ("complete", "needs_operator"):
        receipt = json.loads(row[1] or "{}")
        if receipt.get("status") != row[0]:
            raise PolicyError("missing durable result")
        if row[0] == "complete" and receipt.get("epoch") != epoch:
            raise PolicyError("receipt belongs to another manager generation")


def main() -> int:
    import sys
    try:
        if sys.argv[1:] == ["allow-local"]:
            allow_local(os.getcwd())
        elif sys.argv[1:] == ["receipt"]:
            active = json.loads(os.environ["CFLOW_DATA_ACTIVE"])
            verify_receipt(os.getcwd(), active["request_id"], active["epoch"])
        else:
            print("usage: python -m claude_launcher.local_sync allow-local|receipt", file=sys.stderr)
            return 2
    except (reposync.SyncError, sqlite3.Error, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
