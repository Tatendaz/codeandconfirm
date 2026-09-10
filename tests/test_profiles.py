"""Profiles: one committed config, two modes (pr per change, full weekly); the gate's diff-scenario rule."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from codeandconfirm.config import Config, DEFAULTS, EXAMPLE_CONFIG, REPO_CONFIG_NAME, apply_profile
from codeandconfirm.gate import evaluate, platform_touched, approve, check_approval


def _repo(tmp_path: Path, toml: str) -> Path:
    repo = tmp_path / "repo"; repo.mkdir(parents=True); (repo / ".git").mkdir()
    (repo / REPO_CONFIG_NAME).write_text(toml)
    return repo


def test_example_profiles_pr_default_and_full(tmp_path):
    repo = _repo(tmp_path, EXAMPLE_CONFIG)
    pr = Config.load(repo_root=repo)
    assert pr.get("profile.name") == "pr" and "diff" in pr.get("profile.description")
    assert pr.get("codex.reasoning_effort") == "medium"
    assert pr.get("qa.required_journeys") == ["auth.signup"]
    assert pr.get("qa.require_diff_scenarios") is True and pr.get("qa.base_comparison") is False
    assert pr.get("qa.worker_timeout_minutes") == 15
    assert pr.get("perf.enabled") is False
    full = Config.load(repo_root=repo, profile="full")
    assert full.get("profile.name") == "full"
    assert full.get("codex.reasoning_effort") == "high"
    assert len(full.get("qa.required_journeys")) == 5 and full.get("perf.enabled") is True
    assert full.get("qa.require_diff_scenarios") is False          # untouched keys keep the base value
    assert full.get("qa.required_suites") == pr.get("qa.required_suites") == ["ios-ui", "android-ui"]


def test_unknown_profile_is_an_error_and_no_profiles_means_no_change(tmp_path):
    repo = _repo(tmp_path, EXAMPLE_CONFIG)
    with pytest.raises(ValueError, match="unknown profile 'weekly'"):
        Config.load(repo_root=repo, profile="weekly")
    plain = _repo(tmp_path / "plain", '[project]\nname = "x"\n')
    cfg = Config.load(repo_root=plain)
    assert cfg.get("profile.name") == "" and cfg.get("codex.reasoning_effort") == "high"
    d = apply_profile(copy.deepcopy(DEFAULTS), None)
    assert d["profile"]["name"] == ""


def test_profile_changes_the_approval_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, EXAMPLE_CONFIG)
    pr, full = Config.load(repo_root=repo), Config.load(repo_root=repo, profile="full")
    from test_gate import CAND
    from codeandconfirm.gate import GateResult
    rec = approve("run-1", str(repo), CAND, pr, GateResult("PASS"), {"profile": "pr"})
    assert rec.exists()
    assert check_approval(repo, CAND["candidate_sha"], pr)["approved"]
    r = check_approval(repo, CAND["candidate_sha"], full)
    assert not r["approved"] and "configuration changed" in r["reason"]     # a pr PASS is not a full PASS


def test_platform_touched_ignores_tests_and_docs():
    assert platform_touched(["ios/App/Views/Auth.swift"], "ios")
    assert not platform_touched(["ios/App/Views/Auth.swift"], "android")
    assert platform_touched(["android/app/src/main/java/x/Trees.kt"], "android")
    assert not platform_touched(["ios/App/AppUITests/AppUITests.swift", "docs/features/x.md"], "ios")
    assert not platform_touched(["android/app/src/androidTest/java/x/UiTest.kt"], "android")
    # unit-test targets and test files by name are tests too; names that merely contain "test" are product code
    assert not platform_touched(["ios/AppTests/FooTests.swift", "ios/AppTests/Support/Fixtures.swift"], "ios")
    assert not platform_touched(["android/lib/src/test/java/x/FooTest.kt", "android/app/src/main/java/x/FooTests.kt"], "android")
    assert not platform_touched(["firebase/functions/lib/sweep.test.js", "firebase/functions/lib/rules.spec.ts"], "ios")
    assert platform_touched(["ios/App/Latest/View.swift"], "ios") and platform_touched(["android/app/src/main/java/x/Latest.kt"], "android")
    assert platform_touched(["firebase/functions/lib/contest.js"], "android")
    assert platform_touched(["firebase/firestore.rules"], "ios") and platform_touched(["firebase/firestore.rules"], "android")
    assert platform_touched(["firebase/functions/index.js"], "android")
    # operations tooling, configs and CI next to product code ask for no device scenario
    assert not platform_touched(["firebase/functions/scripts/sweep.js", "firebase/functions/scripts/cleanup.js"], "ios")
    assert not platform_touched(["firebase/config/README.md", "codeandconfirm.toml", ".github/workflows/x.yml", "android/app/google-services.json"], "android")
    assert not platform_touched([], "ios")


def test_gate_requires_a_diff_scenario_only_where_the_diff_touches_product_code(tmp_path):
    from test_gate import make_cfg, good_ctx, CAND
    cfg = make_cfg(tmp_path, qa={"require_diff_scenarios": True})
    ctx = good_ctx(tmp_path)
    cand = dict(CAND, changed_files=["ios/App/Views/Person.swift"])
    g = evaluate(cfg, cand, ctx)
    assert g.verdict == "BLOCKED"
    assert any("evidence.ios.diff-scenarios" in r and "no diff.* scenario" in r for r in g.block_reasons)
    # the worker reports one diff-derived scenario with evidence → PASS
    shot = next(Path(ctx["evidence_root"], "ios").glob("*.png")).name
    ctx["workers"]["ios"]["verdict"]["scenarios"].append({"id": "diff.person-edit", "status": "passed", "evidence": [shot], "notes": ""})
    assert evaluate(cfg, cand, ctx).verdict == "PASS"
    # a test-only / docs-only diff needs none
    ctx2 = good_ctx(tmp_path / "b")
    g2 = evaluate(cfg, dict(CAND, changed_files=["ios/AppUITests/Smoke.swift", "README.md"]), ctx2)
    assert g2.verdict == "PASS"
    chk = next(c for c in g2.checks if c.name == "evidence.ios.diff-scenarios")
    assert chk.ok and not chk.required and "no ios product code" in chk.detail
