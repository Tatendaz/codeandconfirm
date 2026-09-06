"""Optional GitHub publication: a PR comment and/or a commit status on the tested head SHA.

Nothing here runs unless `--publish` is passed or `[github].publish = true`. Publishing a status
lets branch protection require the context (default `codeandconfirm/qa`) as the merge backstop.
"""

from __future__ import annotations

from pathlib import Path

from .util import run, which


def _gh_ok() -> bool:
    return bool(which("gh")) and run(["gh", "auth", "status"], timeout=30).ok


def publish_comment(repo_slug: str, number: int, report_md: Path, max_chars: int = 60000) -> dict:
    if not _gh_ok():
        return {"ok": False, "error": "gh not installed or not authenticated"}
    body = report_md.read_text()
    if len(body) > max_chars:
        body = body[:max_chars] + "\n\n_(report truncated; full report in the run directory)_\n"
    body = "<!-- codeandconfirm-report -->\n" + body
    r = run(["gh", "pr", "comment", str(number), "--repo", repo_slug, "--body-file", "-"], input_text=body, timeout=60)
    return {"ok": r.ok, "output": (r.stdout or r.stderr).strip()[:300]}


def publish_status(repo_slug: str, sha: str, verdict: str, context: str, description: str, target_url: str | None = None) -> dict:
    if not _gh_ok():
        return {"ok": False, "error": "gh not installed or not authenticated"}
    state = {"PASS": "success", "FAIL": "failure", "BLOCKED": "error", "CANCELLED": "error"}.get(verdict, "error")
    args = ["gh", "api", "-X", "POST", f"repos/{repo_slug}/statuses/{sha}", "-f", f"state={state}", "-f", f"context={context}",
            "-f", f"description={description[:130]}"]
    if target_url:
        args += ["-f", f"target_url={target_url}"]
    r = run(args, timeout=60)
    return {"ok": r.ok, "state": state, "output": (r.stdout or r.stderr).strip()[:300]}


def repo_slug_from_remote(repo_root: Path) -> str | None:
    r = run(["git", "-C", str(repo_root), "remote", "get-url", "origin"], timeout=15)
    if not r.ok:
        return None
    url = r.stdout.strip()
    for pat in ("github.com:", "github.com/"):
        if pat in url:
            slug = url.split(pat, 1)[1]
            return slug.removesuffix(".git").strip("/")
    return None
