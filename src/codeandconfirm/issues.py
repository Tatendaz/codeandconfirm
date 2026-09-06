"""Opt-in publication of a run's findings as GitHub issues (`codeandconfirm publish-issues <run-id>`).

Nothing here runs automatically. Each finding becomes one issue with the severity, platform, category,
description, numbered reproduction steps, evidence paths, base comparison and the suggested regression
test from the report. A hidden marker `<!-- codeandconfirm-finding:<key> -->` in the body and a ledger in
the run directory (`published-issues.json`) make the command idempotent: re-running never files the same
finding twice, and an issue that already carries the marker (from any run) is reused.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .gate import SEVERITY_ORDER, sev_rank
from .github import repo_slug_from_remote, _gh_ok
from .runstore import Run
from .util import now_iso, read_json, redact, run, write_json

MARKER = "codeandconfirm-finding"


def finding_key(f: dict) -> str:
    """Stable identity of a finding across runs: platform + title (normalised); the run id is deliberately
    not part of it so a re-run of the same branch does not file duplicates."""
    basis = f"{(f.get('platform') or '').lower()}|{' '.join(str(f.get('title', '')).lower().split())}"
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def issue_title(f: dict) -> str:
    plat = f.get("platform") or "n/a"
    prefix = "" if plat in ("n/a", "both") else f"{plat}: "
    return f"[{str(f.get('severity', 'info')).lower()}] {prefix}{str(f.get('title', '')).strip()}"[:240]


def issue_body(f: dict, report: dict, run_id: str, run_dir: Path) -> str:
    cand = report.get("candidate") or {}
    ev_dir = run_dir / "evidence" / str(f.get("worker") or f.get("platform") or "")
    L = [f"**Severity:** {f.get('severity')} · **Platform:** {f.get('platform')} · **Area:** {f.get('category')}"
         + (f" · **Classification:** {f.get('classification')}" if f.get("classification") else "")]
    src = (f"PR #{cand['pr'].get('number')} ({cand['pr'].get('url')})" if cand.get("pr") else f"branch `{cand.get('candidate_ref')}`")
    L.append("")
    L.append(f"Found by CodeAndConfirm hands-on QA of {src} — run `{run_id}`, candidate `{str(cand.get('candidate_sha', ''))[:12]}` "
             f"vs base `{str(cand.get('base_sha', ''))[:12]}`.")
    L.append("")
    L.append("## What happens")
    L.append(redact(str(f.get("description") or "")).strip() or "(no description)")
    if f.get("reproduction"):
        L.append("")
        L.append("## Reproduction")
        for i, step in enumerate(f["reproduction"], 1):
            L.append(f"{i}. {redact(str(step))}")
    if f.get("evidence"):
        L.append("")
        L.append(f"## Evidence (local run directory)")
        L.append(f"`{ev_dir}`: " + ", ".join(f"`{e}`" for e in f["evidence"]))
    if f.get("compared_to_base") and f.get("compared_to_base") != "not compared":
        L.append("")
        L.append("## Compared to base")
        L.append(redact(str(f["compared_to_base"])))
    if f.get("suggested_regression_test"):
        L.append("")
        L.append("## Suggested regression test")
        L.append(redact(str(f["suggested_regression_test"])))
    L.append("")
    L.append(f"<!-- {MARKER}:{finding_key(f)} -->")
    return "\n".join(L) + "\n"


def select_findings(report: dict, min_severity: str) -> list[dict]:
    if min_severity not in SEVERITY_ORDER:
        raise ValueError(f"min_severity must be one of {SEVERITY_ORDER}")
    thr = sev_rank(min_severity)
    fs = [f for f in ((report.get("gate") or {}).get("all_findings") or []) if sev_rank(f.get("severity", "info")) >= thr]
    return sorted(fs, key=lambda x: -sev_rank(x.get("severity", "info")))


def _existing_issue(repo_slug: str, key: str) -> dict | None:
    r = run(["gh", "issue", "list", "--repo", repo_slug, "--state", "all", "--search", f'"{MARKER}:{key}" in:body',
             "--json", "number,url,title,state", "--limit", "5"], timeout=60)
    if not r.ok:
        return None
    try:
        hits = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return hits[0] if hits else None


def publish_issues(run_id: str, *, repo_slug: str | None = None, labels: list[str] | None = None, min_severity: str = "low",
                   dry_run: bool = False, log=lambda s: None) -> dict:
    r = Run(run_id)
    report = read_json(r.dir / "report.json", None)
    if not report:
        raise FileNotFoundError(f"run {run_id} has no report.json yet (status {r.state.status})")
    cand = report.get("candidate") or {}
    slug = repo_slug or (cand.get("pr") or {}).get("repo") or repo_slug_from_remote(Path(r.state.repo_root))
    if not slug:
        raise ValueError("cannot determine the GitHub repository; pass --repo-slug owner/repo")
    ledger_path = r.dir / "published-issues.json"
    ledger = read_json(ledger_path, {}) or {}
    out = {"run_id": run_id, "repo": slug, "dry_run": dry_run, "issues": []}
    findings = select_findings(report, min_severity)
    if not findings:
        log(f"no findings at/above {min_severity} in run {run_id}")
        return out
    if not dry_run and not _gh_ok():
        raise RuntimeError("gh is not installed or not authenticated")
    for f in findings:
        key = finding_key(f)
        title = issue_title(f)
        entry = {"key": key, "title": title, "severity": f.get("severity"), "platform": f.get("platform")}
        if key in ledger:
            entry.update(action="skipped-published", url=ledger[key].get("url")); out["issues"].append(entry)
            log(f"skip (already filed from this run): {title} → {ledger[key].get('url')}"); continue
        body = issue_body(f, report, run_id, r.dir)
        if dry_run:
            entry.update(action="dry-run", body=body); out["issues"].append(entry)
            log(f"would create: {title}"); continue
        ex = _existing_issue(slug, key)
        if ex:
            ledger[key] = {"url": ex.get("url"), "number": ex.get("number"), "reused": True, "at": now_iso()}
            entry.update(action="skipped-existing", url=ex.get("url")); out["issues"].append(entry)
            log(f"skip (issue exists): {title} → {ex.get('url')}"); continue
        args = ["gh", "issue", "create", "--repo", slug, "--title", title, "--body-file", "-"]
        for lb in labels or []:
            args += ["--label", lb]
        res = run(args, input_text=body, timeout=90)
        if res.ok:
            url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
            ledger[key] = {"url": url, "title": title, "at": now_iso()}
            entry.update(action="created", url=url); log(f"created: {title} → {url}")
        else:
            entry.update(action="failed", error=(res.stderr or res.stdout).strip()[:300]); log(f"FAILED: {title}: {entry['error']}")
        out["issues"].append(entry)
        write_json(ledger_path, ledger)
    if not dry_run:
        write_json(ledger_path, ledger)
    return out
