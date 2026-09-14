"""Is the GitHub CLI here, and is it signed in to the hosts this fleet pushes to?

``improv-worker-remote`` lands a round as a pull request, and the whole of
its remote step is three facts about the machine the daemon runs on: ``gh``
resolves on the daemon's PATH (a session's child is spawned with that same
environment, so a miss here is a miss in every worker), ``gh auth status``
succeeds for the host each repository's ``claunch.pr.remote`` points at, and
that config key is set at all. A worker that finds any of the three missing
stops and asks the user -- it cannot install a client or sit through an
interactive login. This module is the same three questions asked *before* a
worker is spawned, so the Settings page can show the answer and the user can
act on it once instead of once per blocked round.

Everything here is a read: no install, no login, no config write. The guide
it returns is the list of commands the user runs themselves, chosen from what
is actually missing -- a machine that has ``gh`` and is signed in to every
host it needs gets an empty guide, and a card with nothing to say.

Hosts are not guessed from ``origin``. A repository with two remotes (the
public mirror and the enterprise host is the common shape) says which one
lands its pull requests through ``git config claunch.pr.remote``; when that
key is unset every remote's host is listed with ``configured: false`` so the
user sees the choice they still have to make, and the guide spells the
``git config`` line for it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: The executable, resolved on the daemon's PATH (``shutil.which`` honours
#: PATHEXT, so ``gh.exe`` on Windows resolves from the bare name).
BINARY = "gh"

#: The git config keys ``improv-worker-remote`` reads; spelled once here and
#: once in the workflow's prose. A drift between the two is a worker asking
#: the user for a remote the card said was configured.
REMOTE_KEY = "claunch.pr.remote"
BASE_KEY = "claunch.pr.base"

#: Seconds a single ``gh``/``git`` call may take. ``gh auth status`` talks to
#: the host, and a host that is down must not hold the Settings page.
TIMEOUT = 8.0


def install_command(platform: Optional[str] = None) -> str:
    """The one-line install for this platform, as the guide prints it."""
    plat = platform or sys.platform
    if plat == "win32":
        return "winget install --id GitHub.cli"
    if plat == "darwin":
        return "brew install gh"
    return "https://github.com/cli/cli/blob/trunk/docs/install_linux.md"


def _run(
    argv: Sequence[str], cwd: Optional[str] = None, env: Optional[dict] = None
) -> Tuple[int, str, str]:
    """Run one command; a missing binary or a hang is a failure, not a crash."""
    try:
        proc = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT, env=env,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {TIMEOUT:g}s: {' '.join(argv)}"
    except OSError as exc:
        return 126, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# the client
# --------------------------------------------------------------------------- #
def client(which=shutil.which) -> dict:
    """Whether ``gh`` resolves, where, and which version."""
    path = which(BINARY)
    if not path:
        return {"installed": False, "path": None, "version": None}
    code, out, err = _run([path, "--version"])
    first = (out or err).strip().splitlines()
    line = first[0].strip() if first else ""
    if code != 0:
        return {
            "installed": True, "path": path, "version": line or None,
            "error": (err or out).strip() or f"gh --version exited {code}",
        }
    m = re.search(r"\bversion\s+(\S+)", line)
    return {"installed": True, "path": path, "version": m.group(1) if m else (line or None)}


# --------------------------------------------------------------------------- #
# the hosts
# --------------------------------------------------------------------------- #
_URL = re.compile(r"^([a-z][a-z0-9+.-]*)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", re.I)
_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):([^/].*?)(?:\.git)?/?$")


def parse_remote_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """``(host, owner/repo)`` from a remote URL in any of git's spellings.

    Returns ``(None, None)`` for a local path remote (``file://``, an
    absolute path, a Windows drive) -- there is no host to sign in to, and
    the card should not list one.
    """
    url = (url or "").strip()
    m = _URL.match(url)
    if m:
        scheme, host, path = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if scheme == "file" or host == "localhost":
            return None, None
        return host, path
    if re.match(r"^[A-Za-z]:[\\/]", url) or url.startswith(("/", ".", "\\")):
        return None, None
    m = _SCP.match(url)
    if m:
        return m.group(1).lower(), m.group(2)
    return None, None


def _git(cwd: str, *args: str) -> Tuple[int, str]:
    code, out, _ = _run(["git", "-C", cwd, *args])
    return code, out.strip()


def repository_hosts(cwd: str) -> List[dict]:
    """The GitHub hosts one repository could open pull requests on.

    With ``claunch.pr.remote`` set, exactly that remote's host, marked
    ``configured``. Without it, every remote that has a host, none marked --
    the choice is the user's, and the guide says how to record it. A
    directory that is not a repository contributes nothing.
    """
    code, _ = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return []
    _, chosen = _git(cwd, "config", "--get", REMOTE_KEY)
    _, base = _git(cwd, "config", "--get", BASE_KEY)
    code, listing = _git(cwd, "remote")
    if code != 0:
        return []
    remotes = [r.strip() for r in listing.splitlines() if r.strip()]
    rows: List[dict] = []
    for remote in remotes:
        if chosen and remote != chosen:
            continue
        _, url = _git(cwd, "remote", "get-url", remote)
        host, slug = parse_remote_url(url)
        if not host:
            continue
        rows.append({
            "host": host, "remote": remote, "slug": slug,
            "configured": bool(chosen), "base": base or "master",
        })
    if chosen and not rows:
        # The key names a remote this repository does not have (renamed,
        # removed, or a local path): the worker fails on exactly this line,
        # so the card says it first.
        rows.append({
            "host": None, "remote": chosen, "slug": None,
            "configured": True, "base": base or "master",
            "error": (
                f"{REMOTE_KEY} = {chosen!r}, but this repository has no GitHub "
                f"remote of that name (remotes: {', '.join(remotes) or 'none'})"
            ),
        })
    return rows


_ACCOUNT = re.compile(r"[Ll]ogged in to \S+ (?:account|as) (\S+)")


def auth_status(gh_path: str, host: str) -> dict:
    """``gh auth status --hostname <host>``, read for the two things that
    matter: did it succeed, and as whom."""
    env = dict(os.environ)
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("NO_COLOR", "1")
    code, out, err = _run([gh_path, "auth", "status", "--hostname", host], env=env)
    text = "\n".join(s for s in (out, err) if s).strip()
    m = _ACCOUNT.search(text)
    account = m.group(1).rstrip(".") if m else None
    # gh prints one verdict line per host, decorated with a check or a cross;
    # keep the line that names the host, without the decoration.
    lines = [ln.strip().lstrip("✓✗X- ").strip() for ln in text.splitlines() if ln.strip()]
    detail = next((ln for ln in lines if host in ln), lines[0] if lines else "")
    return {"authenticated": code == 0, "account": account, "detail": detail[:300]}


# --------------------------------------------------------------------------- #
# the whole answer
# --------------------------------------------------------------------------- #
def token_env_set() -> bool:
    """Whether a non-interactive token is in the daemon's environment. Only
    the fact; the value never leaves the process."""
    return bool(os.environ.get("GH_ENTERPRISE_TOKEN") or os.environ.get("GH_TOKEN"))


def status(
    repositories: Iterable[Tuple[str, str]],
    *,
    which=shutil.which,
    auth=auth_status,
    hosts_of=repository_hosts,
    platform: Optional[str] = None,
) -> dict:
    """The card's whole payload.

    ``repositories`` is ``(label, path)`` pairs -- the registered workspaces
    and the daemon's own directory, which is what the create form offers.
    The three probes are injectable so the tests can describe a machine
    without having one.
    """
    cli = client(which)
    repos: List[dict] = []
    by_host: Dict[str, dict] = {}
    unconfigured: List[str] = []
    broken: List[str] = []
    for label, path in repositories:
        rows = hosts_of(path)
        if not rows:
            continue
        repos.append({"name": label, "path": path, "remotes": rows})
        if not any(r["configured"] for r in rows):
            unconfigured.append(label)
        for r in rows:
            if r.get("error"):
                broken.append(f"{label}: {r['error']}")
                continue
            entry = by_host.setdefault(r["host"], {"host": r["host"], "repositories": []})
            entry["repositories"].append(label)
    hosts: List[dict] = []
    for host, entry in sorted(by_host.items()):
        if cli["installed"]:
            entry.update(auth(cli["path"], host))
        else:
            entry.update({"authenticated": False, "account": None,
                          "detail": "gh is not installed"})
        hosts.append(entry)

    guide: List[dict] = []
    if not cli["installed"]:
        guide.append({
            "why": "gh is not on the daemon's PATH, so every improv-worker-remote "
                   "round stops at remote-setup until it is.",
            "run": install_command(platform),
            "note": "Restart the daemon afterwards so its PATH sees the new binary.",
        })
    for h in hosts:
        if h["authenticated"] or not cli["installed"]:
            # a login guide before the install guide is noise: gh is what
            # does the logging in
            continue
        guide.append({
            "why": f"{h['host']}: not signed in ({h['detail'] or 'no session'}).",
            "run": f"gh auth login --hostname {h['host']}",
            "note": "Interactive, once, in your own shell. For a daemon that runs "
                    "unattended, GH_ENTERPRISE_TOKEN (GH_TOKEN for github.com) in "
                    "its environment does the same without a prompt.",
        })
    for label in unconfigured:
        guide.append({
            "why": f"{label}: {REMOTE_KEY} is unset, so a remote worker there has to "
                   "ask which remote its pull requests go to.",
            "run": f"git config {REMOTE_KEY} <remote name>",
            "note": f"Run it in that repository. {BASE_KEY} <branch> as well when "
                    "the base is not master.",
        })
    for line in broken:
        guide.append({
            "why": line,
            "run": f"git config {REMOTE_KEY} <an existing remote>",
            "note": "",
        })
    ready = (
        cli["installed"] and bool(hosts)
        and all(h["authenticated"] for h in hosts)
        and not unconfigured and not broken
    )
    return {
        "client": cli,
        "platform": platform or sys.platform,
        "token_env": token_env_set(),
        "hosts": hosts,
        "repositories": repos,
        "guide": guide,
        "ready": ready,
    }


def daemon_repositories() -> List[Tuple[str, str]]:
    """What the daemon itself would list: its own directory (the create
    form's only offer when nothing is registered) and the registered
    workspaces that exist right now."""
    from . import workspaces

    seen: Dict[str, str] = {}
    own = str(Path(os.getcwd()).resolve())
    seen[own] = "(daemon directory)"
    for w in workspaces.list_all():
        key = str(Path(w.path).resolve())
        if key not in seen and w.exists():
            seen[key] = w.name
    return [(label, path) for path, label in seen.items()]
