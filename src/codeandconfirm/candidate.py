"""Candidate resolution: what exactly is under test, and an isolated checkout of it.

Sources:
  branch  — a local branch (or any committish) in the target repo, vs a base ref
  pr      — a GitHub PR (URL or number): head/base SHAs resolved through `gh`, fetched into the
            run's own worktree; the author's working copy is never touched.

The checkout is a detached `git worktree` owned by the run, so the candidate SHA cannot drift
while QA runs, and Codex cannot silently modify the branch being certified.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .util import run, CommandFailed, now_iso, which


@dataclass
class Candidate:
    source: str                 # branch | pr
    repo_root: str              # the developer's repo (for branch) or the run's fetch repo (for pr)
    candidate_ref: str
    candidate_sha: str
    base_ref: str
    base_sha: str
    merge_base_sha: str
    changed_files: list[str]
    diff_stat: str
    acceptance_criteria: str = ""
    pr: dict | None = None      # number, url, title, author, head_repo, base_repo, is_fork, head_ref
    tested_ref_kind: str = "head"   # head | merge-candidate
    trust: str = "trusted"      # trusted | untrusted
    resolved_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


def _git(repo: Path, *args: str, check: bool = True, timeout: float = 120):
    r = run(["git", "-C", str(repo), *args], timeout=timeout)
    if check and not r.ok:
        raise CommandFailed(r)
    return r


def rev_parse(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def resolve_branch(repo: Path, branch: str, base: str, acceptance: str = "") -> Candidate:
    cand_sha = rev_parse(repo, branch)
    # Prefer the remote-tracking base when it exists: that's what the PR will diff against.
    base_sha = None
    for cand in (f"origin/{base}", base):
        try:
            base_sha = rev_parse(repo, cand); base_ref = cand; break
        except CommandFailed:
            continue
    if base_sha is None:
        raise ValueError(f"cannot resolve base {base!r}")
    mb = _git(repo, "merge-base", base_sha, cand_sha).stdout.strip()
    files = _git(repo, "diff", "--name-only", f"{mb}..{cand_sha}").stdout.split()
    stat = _git(repo, "diff", "--stat", f"{mb}..{cand_sha}").stdout.strip()
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    c = Candidate("branch", str(repo), branch, cand_sha, base_ref, base_sha, mb, files, stat, acceptance)
    if dirty and rev_parse(repo, "HEAD") == cand_sha:
        c.acceptance_criteria = (acceptance + "\n" if acceptance else "") + \
            "NOTE: the developer's working tree had uncommitted changes; only COMMITTED changes are under test."
    return c


_PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def parse_pr(spec: str, default_repo: str | None = None) -> tuple[str, int]:
    m = _PR_URL.search(spec)
    if m:
        return f"{m.group(1)}/{m.group(2).removesuffix('.git')}", int(m.group(3))
    if spec.isdigit() and default_repo:
        return default_repo, int(spec)
    m = re.match(r"([^/\s]+/[^#\s]+)#(\d+)$", spec)
    if m:
        return m.group(1), int(m.group(2))
    raise ValueError(f"cannot parse PR spec {spec!r}; use a URL, owner/repo#N, or N with --repo")


def resolve_pr(spec: str, fetch_repo: Path, default_repo: str | None = None, acceptance: str = "",
               merge_candidate: bool = False) -> Candidate:
    """Resolve a PR's exact head/base via `gh`, fetch both into `fetch_repo` (a run-owned clone or
    the developer's repo, read-only fetch), and return the candidate. The developer's checkout is
    never modified: only refs under refs/codeandconfirm/* are written."""
    if not which("gh"):
        raise RuntimeError("gh CLI is required for --pr")
    repo_slug, number = parse_pr(spec, default_repo)
    r = run(["gh", "pr", "view", str(number), "--repo", repo_slug, "--json",
             "number,url,title,author,headRefName,headRefOid,baseRefName,baseRefOid,isCrossRepository,headRepository,headRepositoryOwner,mergeable,mergeStateStatus,body"],
            timeout=60)
    if not r.ok:
        raise RuntimeError(f"gh pr view failed: {r.stderr.strip()[:300]}")
    info = json.loads(r.stdout)
    head_sha, base_sha = info["headRefOid"], info["baseRefOid"]
    # Fetch the exact head and the base into run-private refs.
    remote_url = f"https://github.com/{repo_slug}.git"
    _git(fetch_repo, "fetch", "--no-tags", remote_url,
         f"+refs/pull/{number}/head:refs/codeandconfirm/pr-{number}-head",
         f"+{info['baseRefName']}:refs/codeandconfirm/pr-{number}-base", timeout=600)
    fetched_head = rev_parse(fetch_repo, f"refs/codeandconfirm/pr-{number}-head")
    if fetched_head != head_sha:
        # The PR moved between `gh pr view` and the fetch. Use what we fetched, and say so.
        head_sha = fetched_head
    tested_kind = "head"
    cand_sha = head_sha
    if merge_candidate:
        try:
            _git(fetch_repo, "fetch", "--no-tags", remote_url, f"+refs/pull/{number}/merge:refs/codeandconfirm/pr-{number}-merge", timeout=600)
            cand_sha = rev_parse(fetch_repo, f"refs/codeandconfirm/pr-{number}-merge")
            tested_kind = "merge-candidate"
        except CommandFailed:
            tested_kind = "head"   # GitHub had no merge ref (conflicts / not computed yet)
    # base for diff purposes = the PR's actual base branch tip (supports non-main bases)
    base_tip = rev_parse(fetch_repo, f"refs/codeandconfirm/pr-{number}-base")
    mb = _git(fetch_repo, "merge-base", base_tip, head_sha).stdout.strip()
    files = _git(fetch_repo, "diff", "--name-only", f"{mb}..{head_sha}").stdout.split()
    stat = _git(fetch_repo, "diff", "--stat", f"{mb}..{head_sha}").stdout.strip()
    is_fork = bool(info.get("isCrossRepository"))
    trust = "untrusted" if is_fork else "trusted"
    body = (info.get("body") or "").strip()
    crit = acceptance or _extract_acceptance(body)
    pr = {"number": number, "url": info["url"], "title": info["title"], "author": (info.get("author") or {}).get("login"),
          "head_ref": info["headRefName"], "base_ref": info["baseRefName"], "is_fork": is_fork,
          "head_repo": (info.get("headRepository") or {}).get("name"), "head_owner": (info.get("headRepositoryOwner") or {}).get("login"),
          "repo": repo_slug, "mergeable": info.get("mergeable"), "merge_state": info.get("mergeStateStatus"),
          "head_sha_at_resolve": info["headRefOid"], "base_sha_at_resolve": base_sha}
    return Candidate("pr", str(fetch_repo), f"refs/pull/{number}/head", cand_sha, info["baseRefName"], base_tip, mb,
                     files, stat, crit, pr, tested_kind, trust)


def _extract_acceptance(body: str) -> str:
    """Pull an 'Acceptance criteria' / 'Testing' section out of a PR body if present."""
    m = re.search(r"(?is)(#+\s*(acceptance|test(ing)? plan|how to test|verification)[^\n]*\n)(.+?)(\n#+\s|\Z)", body)
    return (m.group(1) + m.group(4)).strip() if m else ""


# --- isolated checkout ------------------------------------------------------------


def create_worktree(repo: Path, sha: str, dest: Path) -> Path:
    if dest.exists():
        # a previous attempt may have left a stale directory; git needs it gone or registered
        r = _git(repo, "worktree", "list", "--porcelain", check=False)
        if str(dest) in r.stdout:
            head = rev_parse(dest, "HEAD")
            if head == sha:
                return dest
            _git(repo, "worktree", "remove", "--force", str(dest), check=False)
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(dest), sha, timeout=600)
    return dest


def remove_worktree(repo: Path, dest: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(dest), check=False)
    shutil.rmtree(dest, ignore_errors=True)
    _git(repo, "worktree", "prune", check=False)


def worktree_is_pristine(dest: Path, allowed_prefixes: tuple[str, ...] = ()) -> tuple[bool, list[str]]:
    """True when the candidate checkout has no modifications besides build outputs.

    Codex must not modify the candidate it certifies; any tracked-file change fails the gate."""
    r = _git(dest, "status", "--porcelain", "--untracked-files=no")
    changed = [line[3:] for line in r.stdout.splitlines() if line.strip()]
    offending = [c for c in changed if not any(c.startswith(p) for p in allowed_prefixes)]
    return (not offending), offending


def head_sha(dest: Path) -> str:
    return rev_parse(dest, "HEAD")


def write_diff(repo: Path, base_sha: str, cand_sha: str, dest: Path, max_bytes: int = 4_000_000) -> Path:
    r = _git(repo, "diff", f"{base_sha}..{cand_sha}", timeout=300)
    text = r.stdout
    if len(text.encode()) > max_bytes:
        text = text[:max_bytes] + "\n\n[diff truncated by codeandconfirm]\n"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return dest
