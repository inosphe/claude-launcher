"""Reading, merging and explaining a pull request on behalf of the PR page.

The page (``#/pr``) talks to this daemon and to nothing else. The forge
(``github.ncsoft.com`` on this fleet) is reachable from the daemon host and
not from a browser behind a relay tunnel, so the daemon runs ``gh`` and hands
back the answer. The page keeps one link to the forge -- "open on github" --
for a reader who is on the network that can open it.

Three calls, and one rule that shapes all of them: **a failure says what
failed**. GitHub does not report which files a merge would collide on, and a
daemon without a local checkout of that repository cannot compute them, so
:func:`conflict_files` answers ``computed: False`` with the reason rather
than an empty list that would read as "no conflicts".

- :func:`detail` -- ``gh pr view``, normalised, plus :func:`verdict`: the one
  decision the page's merge button is drawn from.
- :func:`merge` -- ``gh pr merge``, with the failure classified rather than
  echoed, so the page can say "protected branch" beside gh's own words.
- :func:`conflict_files` -- ``git merge-tree --write-tree`` in a local
  checkout, when this daemon has one.

Nothing here decides *whether* a person may merge; that is the route's job
(``daemon/api.py``: a dashboard-cookie request, and only that).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .. import ghcli
from . import paths

#: A read may wait for a slow enterprise host; a merge waits much longer.
READ_TIMEOUT = 20.0
MERGE_TIMEOUT = 180.0
#: A local ``git fetch`` before a merge-tree: the point is a file list, not a
#: complete mirror, so a fetch that drags is abandoned rather than held.
FETCH_TIMEOUT = 45.0

#: The merge methods the route accepts. ``auto`` resolves to whichever the
#: repository allows (see :func:`resolve_method`); the rest are passed to gh.
METHODS = ("auto", "merge", "squash", "rebase")

#: What the page needs and nothing more. A field added here is a field the
#: normaliser below must carry, or the page silently loses it.
FIELDS = (
    "number,title,state,isDraft,url,baseRefName,headRefName,headRefOid,mergeable,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,author,updatedAt,mergeCommit"
)

#: A commit SHA a merge may be pinned to. ``gh pr merge --match-head-commit``
#: and the comparison before it use the same full, lower-case form.
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")

#: gh's own words, in the page's language, for each failure class.
SENTENCES = {
    "conflict": "기준선과 충돌합니다. 이 PR을 rebase한 뒤 다시 push해야 머지할 수 있습니다.",
    "protected": "저장소의 보호 규칙이 머지를 막았습니다(리뷰 승인·필수 체크·서명 등).",
    "permission": "이 계정에 그 PR을 머지할 권한이 없습니다.",
    "rate-limit": "GitHub의 요청 한도에 걸렸습니다. 잠시 뒤에 다시 시도하세요.",
    "network": "GitHub에 닿지 못했습니다(네트워크·프록시·호스트 주소).",
    "not-found": "그 PR이나 저장소를 찾지 못했습니다.",
    "not-mergeable": "지금 상태로는 머지할 수 없습니다.",
    "bad-output": "gh의 응답을 읽지 못했습니다.",
    "head-moved": ("판정을 받은 뒤 PR에 새 커밋이 올라왔습니다 — 화면이 보여 준 커밋과 다른 커밋이 "
                   "머지될 뻔했습니다. PR을 다시 열어 새 head를 확인한 뒤 머지하세요."),
    "other": "gh가 실패했습니다. 아래 원문을 확인하세요.",
}

#: The verdicts :func:`verdict` can return. ``clean`` is the only one the page
#: draws an enabled merge button for.
CLEAN = "clean"

_FAILED_CONCLUSIONS = {
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
}
_PENDING_STATUSES = {
    "QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED",
}


# --------------------------------------------------------------------------- #
# running things
# --------------------------------------------------------------------------- #
def _run(argv: Sequence[str], cwd: Optional[str] = None, timeout: float = READ_TIMEOUT
         ) -> Tuple[int, str, str]:
    """One command; a missing binary or a hang is a failure, not a crash."""
    env = dict(os.environ)
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        proc = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s: {' '.join(argv)}"
    except OSError as exc:
        return 126, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _gh_path() -> Optional[str]:
    return shutil.which(ghcli.BINARY)


def slug(host: str, owner: str, name: str) -> str:
    """``host/owner/name`` -- what ``gh -R`` takes."""
    return f"{host}/{owner}/{name}"


# --------------------------------------------------------------------------- #
# the failure classes
# --------------------------------------------------------------------------- #
def classify(*texts: Optional[str]) -> str:
    """Which kind of failure this output is, for :data:`SENTENCES`.

    The order matters: a message that says both "protected branch" and
    "required status check" is the protection talking, and a merge that
    reports a conflict while also naming a check is still a conflict."""
    low = " \n".join(t for t in texts if t).lower()
    if not low.strip():
        return "other"
    if "conflict" in low or "not mergeable" in low or "cannot be merged" in low:
        return "conflict"
    if ("protected branch" in low or "branch protection" in low
            or "required status check" in low or "required review" in low
            or "approving review" in low or "reviews are required" in low):
        return "protected"
    if "rate limit" in low or "secondary rate" in low or "abuse detection" in low:
        return "rate-limit"
    if ("permission" in low or "forbidden" in low or "not authorized" in low
            or "resource not accessible" in low or "must have admin" in low
            or " 403" in low):
        return "permission"
    if ("could not resolve host" in low or "connection refused" in low
            or "connection timed out" in low or "network is unreachable"
            in low or "dial tcp" in low or "tls" in low or "proxy" in low):
        return "network"
    if "not found" in low or " 404" in low:
        return "not-found"
    return "other"


def _failure(kind: str, raw: str = "", message: str = "") -> dict:
    return {
        "kind": kind,
        "message": message or SENTENCES.get(kind, SENTENCES["other"]),
        "raw": (raw or "").strip()[:4000],
    }


# --------------------------------------------------------------------------- #
# the read
# --------------------------------------------------------------------------- #
def summarize_checks(rollup) -> dict:
    """How many checks passed, failed and are still running.

    ``statusCheckRollup`` mixes two shapes: check runs (``status`` +
    ``conclusion``) and commit statuses (``state``). A check that carries
    neither is counted as passing only when it says so; anything unknown is
    left out of all three counts rather than guessed into one."""
    rows = rollup if isinstance(rollup, list) else []
    passing = failing = pending = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        conclusion = str(row.get("conclusion") or "").upper()
        status = str(row.get("status") or "").upper()
        state = str(row.get("state") or "").upper()
        if conclusion in _FAILED_CONCLUSIONS or state in {"FAILURE", "ERROR"}:
            failing += 1
        elif status in _PENDING_STATUSES or state in {"PENDING", "EXPECTED", "IN_PROGRESS"}:
            pending += 1
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"} or state == "SUCCESS":
            passing += 1
    total = passing + failing + pending
    if failing:
        state_word = "failing"
    elif pending:
        state_word = "pending"
    elif total:
        state_word = "passing"
    else:
        state_word = "none"
    return {
        "total": total, "passing": passing, "failing": failing,
        "pending": pending, "state": state_word,
    }


def _author(raw) -> str:
    if isinstance(raw, dict):
        return str(raw.get("login") or "")
    return ""


def _normalize(raw: dict) -> dict:
    merged_commit = raw.get("mergeCommit")
    return {
        "number": int(raw.get("number") or 0),
        "title": str(raw.get("title") or ""),
        "state": str(raw.get("state") or "").upper(),
        "draft": bool(raw.get("isDraft")),
        "url": str(raw.get("url") or ""),
        "base": str(raw.get("baseRefName") or ""),
        "head": str(raw.get("headRefName") or ""),
        "head_sha": str(raw.get("headRefOid") or ""),
        "mergeable": str(raw.get("mergeable") or "").upper(),
        "merge_state": str(raw.get("mergeStateStatus") or "").upper(),
        "review": str(raw.get("reviewDecision") or "").upper(),
        "checks": summarize_checks(raw.get("statusCheckRollup")),
        "author": _author(raw.get("author")),
        "updated_at": str(raw.get("updatedAt") or ""),
        "merge_commit": str((merged_commit or {}).get("oid") or "")
        if isinstance(merged_commit, dict) else "",
    }


def verdict(pr: Optional[dict]) -> dict:
    """Whether the page may draw an enabled merge button, and why not.

    The order is the order a person would ask the questions: is it already
    over, is it a draft, does it collide, is the base ahead, are the checks
    in, is review blocking, and only then "yes".

    ``BEHIND`` is refused rather than merged anyway: a repository that
    requires an up-to-date branch (the common setting on this fleet) fails
    the merge, and a button that sometimes works is worse than a sentence
    that says rebase."""
    if not isinstance(pr, dict):
        return {"can_merge": False, "kind": "unknown", "reason": "PR 상태를 읽지 못했습니다."}
    state = str(pr.get("state") or "").upper()
    if state == "MERGED":
        return {"can_merge": False, "kind": "merged", "reason": "이미 머지된 PR입니다."}
    if state == "CLOSED":
        return {"can_merge": False, "kind": "closed", "reason": "닫힌 PR입니다."}
    if pr.get("draft"):
        return {"can_merge": False, "kind": "draft", "reason": "초안(draft) PR입니다. 초안을 해제한 뒤에 머지할 수 있습니다."}
    mergeable = str(pr.get("mergeable") or "").upper()
    merge_state = str(pr.get("merge_state") or "").upper()
    checks = pr.get("checks") or {}
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        return {"can_merge": False, "kind": "conflict", "reason": SENTENCES["conflict"]}
    if merge_state == "BEHIND":
        return {"can_merge": False, "kind": "behind",
                "reason": f"기준선({pr.get('base') or 'base'})이 이 브랜치보다 앞서 있습니다. rebase한 뒤에 머지하세요."}
    if merge_state == "UNKNOWN" or mergeable == "UNKNOWN":
        return {"can_merge": False, "kind": "unknown",
                "reason": "GitHub가 아직 머지 가능 여부를 계산하지 못했습니다. 잠시 뒤 다시 확인하세요."}
    if checks.get("failing"):
        return {"can_merge": False, "kind": "checks-failing",
                "reason": f"실패한 체크가 {checks['failing']}건 있습니다."}
    if checks.get("pending"):
        return {"can_merge": False, "kind": "checks-pending",
                "reason": f"체크 {checks['pending']}건이 아직 실행 중입니다."}
    if merge_state == "BLOCKED":
        review = str(pr.get("review") or "")
        if review == "REVIEW_REQUIRED":
            return {"can_merge": False, "kind": "review-required",
                    "reason": "필수 리뷰 승인이 아직 없습니다."}
        if review == "CHANGES_REQUESTED":
            return {"can_merge": False, "kind": "changes-requested",
                    "reason": "리뷰에서 변경 요청이 걸려 있습니다."}
        return {"can_merge": False, "kind": "blocked",
                "reason": SENTENCES["protected"]}
    if merge_state in ("CLEAN", "HAS_HOOKS") or mergeable == "MERGEABLE":
        return {"can_merge": True, "kind": CLEAN, "reason": "머지할 수 있습니다."}
    return {"can_merge": False, "kind": "unknown",
            "reason": "머지 가능 여부를 판정하지 못했습니다."}


def detail(host: str, owner: str, name: str, number: int, *, run=None) -> dict:
    """``gh pr view``, normalised, with the verdict attached.

    ``ok: False`` is a read that did not happen (no gh, a failed call, an
    unreadable answer) -- never an exception: the page must keep showing the
    recorded row, with the reason beside it."""
    run = run or _run
    gh = _gh_path()
    if not gh:
        failure = _failure("no-gh", "", "이 데몬의 PATH에 gh가 없어 PR 상태를 읽지 못합니다.")
        return {"ok": False, "pr": None, "verdict": _verdict_of(failure),
                "conflicts": None, "error": failure}
    code, out, err = run(
        [gh, "pr", "view", str(number), "-R", slug(host, owner, name), "--json", FIELDS],
        timeout=READ_TIMEOUT,
    )
    if code != 0:
        kind = classify(err, out)
        failure = _failure(kind, err or out)
        return {"ok": False, "pr": None, "verdict": _verdict_of(failure),
                "conflicts": None, "error": failure}
    try:
        raw = json.loads(out)
    except ValueError:
        failure = _failure("bad-output", out)
        return {"ok": False, "pr": None, "verdict": _verdict_of(failure),
                "conflicts": None, "error": failure}
    if not isinstance(raw, dict):
        failure = _failure("bad-output", out)
        return {"ok": False, "pr": None, "verdict": _verdict_of(failure),
                "conflicts": None, "error": failure}
    pr = _normalize(raw)
    return {"ok": True, "pr": pr, "verdict": verdict(pr), "conflicts": None, "error": None}


def _verdict_of(failure: dict) -> dict:
    return {"can_merge": False, "kind": str(failure.get("kind") or "unknown"),
            "reason": str(failure.get("message") or "")}


# --------------------------------------------------------------------------- #
# the merge
# --------------------------------------------------------------------------- #
#: What a repository's settings say, in the order the resolver prefers.
_METHOD_FLAGS = (("merge", "mergeCommitAllowed"), ("squash", "squashMergeAllowed"),
                 ("rebase", "rebaseMergeAllowed"))


def resolve_method(host: str, owner: str, name: str, *, run=None) -> str:
    """The merge method the repository allows, preferring a merge commit.

    ``gh pr merge`` without a strategy prompts, and a daemon has no terminal;
    so ``auto`` is resolved here to one of the three, and an unreadable
    answer is ``""`` -- the caller then refuses rather than guesses."""
    run = run or _run
    gh = _gh_path()
    if not gh:
        return ""
    code, out, _err = run(
        [gh, "repo", "view", slug(host, owner, name), "--json",
         "mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed"],
        timeout=READ_TIMEOUT,
    )
    if code != 0:
        return ""
    try:
        allowed = json.loads(out)
    except ValueError:
        return ""
    if not isinstance(allowed, dict):
        return ""
    for method, key in _METHOD_FLAGS:
        if allowed.get(key) is True:
            return method
    return ""


def head_sha(host: str, owner: str, name: str, number: int, *, run=None) -> str:
    """The commit the PR's head branch points at right now, or ``""``.

    The dashboard reads this with the detail view and sends it back with the
    merge, so the merge acts on what the person saw (``claunch-ijkge.3.1``).
    An unreadable answer is ``""`` -- the caller then refuses rather than
    merging whatever happens to be there."""
    run = run or _run
    gh = _gh_path()
    if not gh:
        return ""
    code, out, _err = run(
        [gh, "pr", "view", str(number), "-R", slug(host, owner, name),
         "--json", "headRefOid"],
        timeout=READ_TIMEOUT,
    )
    if code != 0:
        return ""
    try:
        raw = json.loads(out)
    except ValueError:
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("headRefOid") or "").strip().lower()


def merge(host: str, owner: str, name: str, number: int, method: str = "auto", *,
          match_head_commit: Optional[str] = None, run=None) -> dict:
    """``gh pr merge``, with the answer classified.

    Returns ``{"ok", "merged", "method", "output", "error"}``. A refused
    merge is ``ok: False`` with ``error.kind`` one of :data:`SENTENCES`'
    keys -- the page shows that sentence beside gh's own words.

    ``match_head_commit`` pins the merge to the commit the page showed the
    person. gh enforces it too (``--match-head-commit``), but the comparison
    here runs first so the refusal says *why* instead of echoing gh: between
    the verdict and the click someone may have pushed to the branch, and a
    merge that silently lands a different commit is the defect this closes
    (``claunch-ijkge.3.1``)."""
    run = run or _run
    wanted = str(method or "auto").lower()
    if wanted not in METHODS:
        return {"ok": False, "merged": False, "method": wanted, "output": "",
                "error": _failure("other", "", f"머지 방식은 {', '.join(METHODS)} 가운데 하나여야 합니다.")}
    pin = str(match_head_commit or "").strip().lower()
    if pin and not SHA_PATTERN.fullmatch(pin):
        return {"ok": False, "merged": False, "method": wanted, "output": "",
                "error": _failure("other", "",
                                  "머지 대상 커밋(match_head_commit)은 40자 SHA여야 합니다.")}
    gh = _gh_path()
    if not gh:
        return {"ok": False, "merged": False, "method": wanted, "output": "",
                "error": _failure("no-gh", "", "이 데몬의 PATH에 gh가 없어 머지를 실행하지 못했습니다.")}
    if pin:
        current = head_sha(host, owner, name, number, run=run)
        if not current:
            return {"ok": False, "merged": False, "method": wanted, "output": "",
                    "error": _failure("other", "",
                                      "지금 PR head를 읽지 못해 머지 대상을 고정하지 못했습니다. 다시 시도하세요.")}
        if current != pin:
            return {"ok": False, "merged": False, "method": wanted, "output": "",
                    "error": _failure("head-moved", "",
                                      SENTENCES["head-moved"] + f" (화면: {pin[:12]}… / 지금: {current[:12]}…)")}
    chosen = wanted
    if chosen == "auto":
        chosen = resolve_method(host, owner, name, run=run)
        if not chosen:
            return {"ok": False, "merged": False, "method": wanted, "output": "",
                    "error": _failure("other", "",
                                      "저장소가 허용하는 머지 방식을 확인하지 못했습니다. 방식을 직접 고르세요.")}
    argv = [gh, "pr", "merge", str(number), "-R", slug(host, owner, name), "--" + chosen]
    if pin:
        argv += ["--match-head-commit", pin]
    code, out, err = run(argv, timeout=MERGE_TIMEOUT)
    if code != 0:
        return {"ok": False, "merged": False, "method": chosen, "output": out.strip(),
                "error": _failure(classify(err, out), err or out)}
    return {"ok": True, "merged": True, "method": chosen, "output": out.strip(),
            "error": None}


def sweep_after_merge(host: str, owner: str, name: str, number: int, *,
                      issues: Iterable[str] = (), owner_label: str = "daemon",
                      run=None) -> dict:
    """Queue the post-merge sweep for a merge the dashboard just made.

    The repository's landing rule is "a merge into master is swept once, by
    the delayed queue": the leader workflow enqueues every merge it makes and
    the queue batches them. A merge started from the PR page went straight to
    master with nothing queued, so its full-suite answer only existed if a
    person remembered to ask for one (``claunch-ijkge.3.2``).

    Returns ``{"state", "job", "ref", "issue", "why"}``. ``state`` is
    ``queued`` when the job is in the queue, ``skipped`` otherwise -- and a
    skip always carries ``why``, because "not enqueued and no reason" is the
    defect this closes. Nothing here raises: the merge already happened, and
    a failure to queue must not turn it into a failed merge."""
    answer = {"state": "skipped", "job": "", "ref": "", "issue": "", "why": ""}
    run = run or _run
    gh = _gh_path()
    if not gh:
        answer["why"] = "이 데몬의 PATH에 gh가 없어 머지 커밋을 읽지 못했습니다."
        return answer
    code, out, _err = run(
        [gh, "pr", "view", str(number), "-R", slug(host, owner, name),
         "--json", "mergeCommit"],
        timeout=READ_TIMEOUT,
    )
    merge_sha = ""
    if code == 0:
        try:
            raw = json.loads(out)
        except ValueError:
            raw = None
        if isinstance(raw, dict) and isinstance(raw.get("mergeCommit"), dict):
            merge_sha = str(raw["mergeCommit"].get("oid") or "").strip().lower()
    if not SHA_PATTERN.fullmatch(merge_sha):
        answer["why"] = "머지 커밋 SHA를 읽지 못했습니다."
        return answer
    root = find_checkout(host, owner, name)
    if not root:
        answer["why"] = "이 데몬에 그 저장소의 로컬 체크아웃이 없습니다."
        return answer
    tool = Path(root) / "tools" / "delayed_sweep.py"
    if not tool.exists():
        answer["why"] = f"로컬 체크아웃에 {tool} 가 없습니다."
        return answer
    named = [str(i) for i in issues if str(i or "").strip()]
    issue = named[0] if named else f"pr-tab:{number}"
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", str(owner_label or "daemon")) or "daemon"
    answer["issue"] = issue
    # The queue checks the SHA against the local object store, and a checkout
    # that has not fetched since the merge does not have the merge commit yet.
    # A failed fetch is not fatal: enqueue then reports what it could not see.
    run(["git", "-C", root, "fetch", "--quiet"], timeout=FETCH_TIMEOUT)
    try:
        from ..delayed_sweep import Queue

        row = Queue().enqueue(str(root), merge_sha, issue, label, str(tool))
    except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
        answer["why"] = f"{type(exc).__name__}: {exc}"
        return answer
    answer.update(state="queued", job=str(row.get("id") or ""),
                  ref=str(row.get("ref") or ""), why="")
    return answer


def record_merge(host: str, owner: str, name: str, number: int, method: str,
                 actor: dict, *, remote: str = "", sweep: Optional[dict] = None) -> None:
    """Append one line to the daemon's merge ledger.

    There is one login on this daemon, so "who" is what the request declared
    (``daemon/actors.py``) together with the address it came from -- enough
    to answer "who merged this and when" after the fact. A failure to write
    the line never fails the merge that already happened.

    ``sweep`` is what :func:`sweep_after_merge` answered: whether the
    post-merge verification job is queued, and if not, why. A merge that was
    swept and one that was not must be distinguishable afterwards without
    replaying the request, so the skip reason is written with the merge."""
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": slug(host, owner, name), "number": int(number), "method": method,
        "actor": actor, "remote": remote,
    }
    if sweep:
        row["sweep"] = sweep
    try:
        path = paths.daemon_dir() / "pr-merges.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def merged_rows(*, limit: int = 200) -> List[dict]:
    """The ledger, newest last. A malformed line is skipped, not fatal."""
    path = paths.daemon_dir() / "pr-merges.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: List[dict] = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# the conflicts
# --------------------------------------------------------------------------- #
def find_checkout(host: str, owner: str, name: str, *, roots=None) -> Optional[str]:
    """A local checkout of that repository on this host, or ``None``.

    ``roots`` is ``(label, path)`` pairs -- by default what the Settings page
    already lists (``ghcli.daemon_repositories``): the registered workspaces
    and the daemon's own directory. The match is on the remote URL, not the
    directory name: a checkout is "of" that repository when one of its
    remotes points at it."""
    pairs = roots if roots is not None else ghcli.daemon_repositories()
    wanted = f"{owner}/{name}".lower()
    for _label, path in pairs:
        if not path:
            continue
        try:
            rows = ghcli.repository_hosts(path)
        except Exception:  # noqa: BLE001 -- a bad remote is not a crash
            continue
        for row in rows:
            if (str(row.get("host") or "").lower() == host.lower()
                    and str(row.get("slug") or "").lower() == wanted):
                return path
    return None


def conflict_files(host: str, owner: str, name: str, base: str, head: str, *,
                   root: Optional[str] = None, run=None) -> dict:
    """The paths a merge would collide on, or the reason they are unknown.

    ``{"computed": bool, "files": [...], "why": str}``. ``files`` is only
    meaningful when ``computed`` is true: an empty list with
    ``computed: False`` means "not measured", and the page says so."""
    run = run or _run
    if not base or not head:
        return {"computed": False, "files": [], "why": "PR의 기준 브랜치나 머리 브랜치를 알 수 없습니다."}
    path = root or find_checkout(host, owner, name)
    if not path:
        return {"computed": False, "files": [],
                "why": "이 데몬에 그 저장소의 로컬 체크아웃이 없어 충돌 파일을 계산하지 못했습니다."}
    if not shutil.which("git"):
        return {"computed": False, "files": [], "why": "이 데몬에 git이 없습니다."}
    temp_ref = f"refs/claunch/pr-{abs(hash((host, owner, name, head))) % (10 ** 10)}"
    code, _out, err = run(
        ["git", "-C", path, "fetch", "--no-tags", "origin",
         f"+refs/heads/{head}:{temp_ref}"],
        timeout=FETCH_TIMEOUT,
    )
    if code != 0:
        return {"computed": False, "files": [],
                "why": f"PR의 머리 브랜치를 가져오지 못했습니다: {err.strip()[:300]}"}
    try:
        code, out, err = run(
            ["git", "-C", path, "merge-tree", "--write-tree", "--name-only",
             f"origin/{base}", temp_ref],
            timeout=FETCH_TIMEOUT,
        )
        if code not in (0, 1):
            return {"computed": False, "files": [],
                    "why": f"git merge-tree가 실패했습니다: {err.strip()[:300]}"}
        files = _merge_tree_paths(out)
        return {"computed": True, "files": files, "why": ""}
    finally:
        run(["git", "-C", path, "update-ref", "-d", temp_ref], timeout=READ_TIMEOUT)


def _merge_tree_paths(out: str) -> List[str]:
    """The conflicted paths ``git merge-tree --write-tree --name-only`` prints.

    The shape is: the tree object on the first line, then one path per line,
    then a blank line and an informational section ("Auto-merging …",
    "CONFLICT (content): …"). A merge without conflicts prints the tree line
    and nothing else. So the list ends at the first blank line, and a message
    is never taken for a path."""
    lines = (out or "").splitlines()
    if not lines:
        return []
    paths, seen = [], set()
    for raw in lines[1:]:
        path = raw.strip()
        if not path:
            break
        if path.startswith("Auto-merging") or path.startswith("CONFLICT"):
            break
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths
