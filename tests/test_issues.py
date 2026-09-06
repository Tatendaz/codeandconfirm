"""publish-issues: opt-in, idempotent, one issue per finding with reproduction and evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeandconfirm import issues
from codeandconfirm.runstore import Run
from codeandconfirm.util import write_json


def _finding(title="Double tap creates two trees", sev="high", plat="android"):
    return {"id": "f1", "severity": sev, "category": "data-integrity", "title": title, "description": "Two rows appear.", "platform": plat,
            "reproduction": ["Open New tree", "Tap Create twice"], "evidence": ["010-a.png", "backend.json"], "compared_to_base": "not compared",
            "suggested_regression_test": "Deliver two taps 80 ms apart; assert one record.", "base_behavior": "not compared", "worker": plat}


def _run(tmp_path, monkeypatch, findings):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    r = Run("run-x", create=True, repo_root=tmp_path)
    write_json(r.dir / "report.json", {"candidate": {"candidate_sha": "a" * 40, "base_sha": "b" * 40, "candidate_ref": "feat/x",
                                                      "pr": {"repo": "acme/app", "number": 7, "url": "https://github.com/acme/app/pull/7"}},
                                       "gate": {"all_findings": findings}})
    return r


def test_title_body_and_key_are_stable_and_carry_the_marker(tmp_path):
    f = _finding()
    assert issues.issue_title(f) == "[high] android: Double tap creates two trees"
    assert issues.issue_title(dict(f, platform="both")) == "[high] Double tap creates two trees"
    k = issues.finding_key(f)
    assert k == issues.finding_key(dict(f, title="  double TAP creates two   trees", id="other", worker="x"))
    assert k != issues.finding_key(dict(f, platform="ios"))
    body = issues.issue_body(f, {"candidate": {"candidate_sha": "a" * 40, "base_sha": "b" * 40, "pr": {"number": 7, "url": "u"}}}, "run-x", tmp_path)
    assert "**Severity:** high" in body and "1. Open New tree\n2. Tap Create twice" in body
    assert "run `run-x`" in body and f"<!-- codeandconfirm-finding:{k} -->" in body
    assert "010-a.png" in body and "Suggested regression test" in body
    for banned in ("Generated with", "Co-Authored", "Claude Code", "🤖"):
        assert banned not in body


def test_select_findings_filters_and_sorts_by_severity():
    rep = {"gate": {"all_findings": [_finding("low one", "low"), _finding("crit", "critical"), _finding("med", "medium")]}}
    assert [f["title"] for f in issues.select_findings(rep, "medium")] == ["crit", "med"]
    assert [f["title"] for f in issues.select_findings(rep, "low")] == ["crit", "med", "low one"]
    with pytest.raises(ValueError):
        issues.select_findings(rep, "urgent")


def test_dry_run_creates_nothing_and_needs_no_gh(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, [_finding()])
    monkeypatch.setattr(issues, "_gh_ok", lambda: (_ for _ in ()).throw(AssertionError("gh must not be consulted in dry run")))
    out = issues.publish_issues("run-x", dry_run=True)
    assert out["repo"] == "acme/app" and [i["action"] for i in out["issues"]] == ["dry-run"]
    assert "Reproduction" in out["issues"][0]["body"]


def test_publish_is_idempotent_across_ledger_and_existing_issues(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, [_finding(), _finding("Stale sign-in error", "low", "ios")])
    calls = []

    class R:
        def __init__(self, ok, out): self.ok, self.stdout, self.stderr = ok, out, ""
    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["gh", "issue", "list"]:
            # the low finding already has an issue carrying its marker
            return R(True, json.dumps([{"number": 3, "url": "https://github.com/acme/app/issues/3"}]) if "Stale" in kw.get("input_text", "") or "ios" in " ".join(argv) and False else "[]")
        return R(True, "https://github.com/acme/app/issues/9\n")
    monkeypatch.setattr(issues, "_gh_ok", lambda: True)
    monkeypatch.setattr(issues, "run", fake_run)
    # first: existing-issue search finds nothing for the high finding; simulate an existing issue for the low one by key
    low_key = issues.finding_key(_finding("Stale sign-in error", "low", "ios"))
    def fake_existing(slug, key):
        return {"number": 3, "url": "https://github.com/acme/app/issues/3"} if key == low_key else None
    monkeypatch.setattr(issues, "_existing_issue", fake_existing)
    out = issues.publish_issues("run-x", labels=["bug"])
    acts = {i["title"]: i for i in out["issues"]}
    assert acts["[high] android: Double tap creates two trees"]["action"] == "created"
    assert acts["[high] android: Double tap creates two trees"]["url"].endswith("/issues/9")
    assert acts["[low] ios: Stale sign-in error"]["action"] == "skipped-existing"
    created = [a for a in calls if a[:3] == ["gh", "issue", "create"]]
    assert len(created) == 1 and "--label" in created[0] and created[0][created[0].index("--repo") + 1] == "acme/app"
    ledger = json.loads((r.dir / "published-issues.json").read_text())
    assert len(ledger) == 2
    # second run: everything is skipped through the ledger, nothing is created
    calls.clear()
    out2 = issues.publish_issues("run-x")
    assert {i["action"] for i in out2["issues"]} == {"skipped-published"}
    assert not [a for a in calls if a[:3] == ["gh", "issue", "create"]]


def test_missing_report_or_repo_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    Run("run-y", create=True, repo_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        issues.publish_issues("run-y", dry_run=True)
    r = _run(tmp_path, monkeypatch, [_finding()])
    rep = json.loads((r.dir / "report.json").read_text()); rep["candidate"].pop("pr"); write_json(r.dir / "report.json", rep)
    monkeypatch.setattr(issues, "repo_slug_from_remote", lambda p: None)
    with pytest.raises(ValueError, match="--repo-slug"):
        issues.publish_issues("run-x", dry_run=True)
