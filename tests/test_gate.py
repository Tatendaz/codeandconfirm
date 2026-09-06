"""Gate tests: a narrative PASS never becomes PASS without evidence; failures are preserved."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codeandconfirm.config import Config, DEFAULTS
from codeandconfirm.gate import evaluate, approve, check_approval, config_fingerprint
from codeandconfirm.util import append_jsonl


def make_cfg(tmp_path: Path, **over) -> Config:
    import copy
    data = copy.deepcopy(DEFAULTS)
    data["qa"].update({"platforms": ["ios"], "required_suites": ["ios-ui"], "required_journeys": ["auth.signup"],
                       "min_interactions_per_platform": 3, "static_review": False})
    data["suites"] = {"ios-ui": {"platform": "ios", "command": "true", "results": {"kind": "xcresult", "path": "x"}}}
    data["platforms"] = {"ios": {"bundle_id": "com.example"}}
    for k, v in over.items():
        data[k].update(v)
    return Config(data, [], tmp_path)


CAND = {"candidate_sha": "a" * 40, "base_sha": "b" * 40, "candidate_ref": "feat/x", "base_ref": "main", "merge_base_sha": "b" * 40,
        "tested_ref_kind": "head", "trust": "trusted"}


def worker(verdict="PASS", scenarios=None, findings=None, started="2000-01-01T00:00:00+00:00", model=("gpt-6-astra",)):
    return {"exit_code": 0, "timed_out": False, "cancelled": False, "started_at": started, "model_used": list(model), "cli_version": "0.1",
            "verdict": {"worker": "ios", "verdict": verdict, "summary": "", "scenarios": scenarios or [], "findings": findings or [],
                        "untested": [], "blockers": [], "test_patches": [], "commands_run": [], "model_self_report": ""}}


def good_ctx(tmp_path: Path, **over) -> dict:
    ev = tmp_path / "evidence" / "ios"
    ev.mkdir(parents=True)
    shot = ev / "010-after-signup.png"
    shot.write_bytes(b"\x89PNG fake")
    started = "2000-01-01T00:00:00+00:00"
    for i in range(4):
        append_jsonl(ev / "actions.jsonl", {"ts": f"2000-01-01T00:00:0{i+1}+00:00", "action": "tap", "ok": True})
    append_jsonl(ev / "actions.jsonl", {"ts": "2000-01-01T00:00:09+00:00", "action": "screenshot", "file": shot.name})
    (ev / "build-identity.json").write_text(json.dumps({"identity_match": True}))
    ctx = {
        "run_started_at": "1999-12-31T00:00:00+00:00", "evidence_root": str(tmp_path / "evidence"), "platform_status": {},
        "checkout_head_start": CAND["candidate_sha"], "checkout_head_end": CAND["candidate_sha"], "pristine": [True, []],
        "builds": {"ios": {"ok": True, "exit_code": 0}}, "installs": {"ios": {"identity_match": True}},
        "proofs": {"ios": {"ok": True, "steps": [1, 2, 3]}},
        "suites": {"ios-ui": {"exit_code": 0, "timed_out": False, "result": {"kind": "xcresult", "parsed": True, "total": 2, "passed": 2, "failed": 0, "errors": 0}}},
        "workers": {"ios": worker(scenarios=[{"id": "auth.signup", "status": "passed", "evidence": [shot.name], "notes": ""}])},
    }
    ctx.update(over)
    return ctx


def test_full_evidence_passes(tmp_path):
    g = evaluate(make_cfg(tmp_path), CAND, good_ctx(tmp_path))
    assert g.verdict == "PASS", [c for c in g.checks if not c.ok]


def test_narrative_pass_without_suite_results_is_blocked(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["suites"]["ios-ui"]["result"] = {"kind": "xcresult", "parsed": False, "total": 0}
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "BLOCKED"
    assert any("suite.ios-ui" in r for r in g.block_reasons)


def test_failed_suite_fails_even_if_worker_says_pass(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["suites"]["ios-ui"]["result"].update(failed=1, passed=1, failed_tests=["t"])
    ctx["suites"]["ios-ui"]["exit_code"] = 65
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "FAIL"


def test_missing_required_suite_is_blocked(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["suites"] = {}
    assert evaluate(make_cfg(tmp_path), CAND, ctx).verdict == "BLOCKED"


def test_passed_journey_without_existing_evidence_is_blocked(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["verdict"]["scenarios"][0]["evidence"] = ["does-not-exist.png"]
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "BLOCKED"
    assert any("journey.auth.signup" in r for r in g.block_reasons)


def test_too_few_interactions_is_blocked(tmp_path):
    ctx = good_ctx(tmp_path)
    g = evaluate(make_cfg(tmp_path, qa={"min_interactions_per_platform": 50}), CAND, ctx)
    assert g.verdict == "BLOCKED"
    assert any("hands-on" in r for r in g.block_reasons)


def test_interactions_before_worker_start_do_not_count(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["started_at"] = "2000-01-01T00:00:08+00:00"  # after the taps
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "BLOCKED"


def test_blocking_finding_fails(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["verdict"]["findings"] = [{"id": "F1", "severity": "high", "category": "correctness", "title": "dup rows",
                                                     "description": "", "platform": "ios", "reproduction": ["tap", "tap"], "evidence": ["010-after-signup.png"],
                                                     "compared_to_base": "", "suggested_regression_test": ""}]
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "FAIL"
    assert g.blocking_findings and g.blocking_findings[0]["concrete"] is True


def test_low_finding_does_not_block(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["verdict"]["findings"] = [{"id": "F1", "severity": "low", "category": "ux", "title": "nit", "description": "",
                                                     "platform": "ios", "reproduction": [], "evidence": [], "compared_to_base": "", "suggested_regression_test": ""}]
    assert evaluate(make_cfg(tmp_path), CAND, ctx).verdict == "PASS"


def test_candidate_modified_during_qa_fails(tmp_path):
    ctx = good_ctx(tmp_path, pristine=[False, ["ios/App.swift"]])
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "FAIL"


def test_sha_drift_fails(tmp_path):
    ctx = good_ctx(tmp_path, checkout_head_end="c" * 40)
    assert evaluate(make_cfg(tmp_path), CAND, ctx).verdict == "FAIL"


def test_model_mismatch_is_blocked(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["model_used"] = ["gpt-5.5"]
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "BLOCKED"
    assert any("model-verified" in r for r in g.block_reasons)


def test_unverifiable_model_is_blocked(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["model_used"] = []
    assert evaluate(make_cfg(tmp_path), CAND, ctx).verdict == "BLOCKED"


def test_worker_timeout_is_blocked(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"].update(timed_out=True, verdict=None)
    assert evaluate(make_cfg(tmp_path), CAND, ctx).verdict == "BLOCKED"


def test_blocked_platform_is_blocked_not_pass(tmp_path):
    ctx = good_ctx(tmp_path, platform_status={"ios": {"blocked": "simulator unavailable"}})
    ctx["workers"] = {}
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "BLOCKED"


def test_cancelled(tmp_path):
    assert evaluate(make_cfg(tmp_path), CAND, good_ctx(tmp_path, cancelled=True)).verdict == "CANCELLED"


def test_exit_code_only_suite(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.data["suites"]["ios-ui"]["results"] = {"kind": "none", "path": ""}
    ctx = good_ctx(tmp_path)
    ctx["suites"]["ios-ui"] = {"exit_code": 0, "timed_out": False, "result": {"kind": "none", "parsed": True, "total": 0}}
    assert evaluate(cfg, CAND, ctx).verdict == "PASS"
    ctx["suites"]["ios-ui"]["exit_code"] = 1
    assert evaluate(cfg, CAND, ctx).verdict == "FAIL"


def test_approval_bound_to_sha_and_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    cfg = make_cfg(tmp_path)
    g = evaluate(cfg, CAND, good_ctx(tmp_path))
    assert g.verdict == "PASS"
    approve("run-1", str(tmp_path), CAND, cfg, g)
    assert check_approval(tmp_path, CAND["candidate_sha"], cfg)["approved"] is True
    # a new head commit has no approval
    assert check_approval(tmp_path, "d" * 40, cfg)["approved"] is False
    # a changed required plan invalidates the approval
    cfg2 = make_cfg(tmp_path, qa={"required_journeys": ["auth.signup", "tree.create-once"]})
    assert config_fingerprint(cfg2) != config_fingerprint(cfg)
    assert check_approval(tmp_path, CAND["candidate_sha"], cfg2)["approved"] is False
    # base moved: still approved, but warned
    res = check_approval(tmp_path, CAND["candidate_sha"], cfg, base_sha="e" * 40)
    assert res["approved"] and "base moved" in res["warning"]


def test_worker_fail_with_only_low_findings_is_advisory(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["verdict"]["verdict"] = "FAIL"
    ctx["workers"]["ios"]["verdict"]["findings"] = [{"id": "F1", "severity": "medium", "category": "ux", "title": "stale error", "description": "",
                                                     "platform": "ios", "reproduction": ["a"], "evidence": ["010-after-signup.png"], "compared_to_base": "not compared", "suggested_regression_test": ""}]
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "PASS"
    adv = [c for c in g.checks if c.name == "worker.ios.verdict"][0]
    assert adv.required is False and "reported, not blocking" in adv.detail


def test_worker_fail_with_failed_required_journey_fails(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["verdict"]["verdict"] = "FAIL"
    ctx["workers"]["ios"]["verdict"]["scenarios"][0]["status"] = "failed"
    assert evaluate(make_cfg(tmp_path), CAND, ctx).verdict == "FAIL"


def test_device_left_on_base_build_is_blocked(tmp_path):
    ctx = good_ctx(tmp_path, final_identity={"ios": {"match": False, "installed": "deadbeef", "candidate": "cafe"}})
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "BLOCKED" and any("final-build" in r for r in g.block_reasons)


def _high(base_behavior):
    return {"id": "F1", "severity": "high", "category": "data-integrity", "title": "dup", "description": "", "platform": "ios",
            "reproduction": ["a"], "evidence": ["010-after-signup.png"], "compared_to_base": "x", "suggested_regression_test": "", "base_behavior": base_behavior}


def test_preexisting_high_finding_is_advisory_by_default(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["verdict"]["findings"] = [_high("same")]
    g = evaluate(make_cfg(tmp_path), CAND, ctx)
    assert g.verdict == "PASS" and any(c.name == "findings.pre-existing" for c in g.checks)


def test_preexisting_high_finding_blocks_when_configured(tmp_path):
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["verdict"]["findings"] = [_high("same")]
    assert evaluate(make_cfg(tmp_path, qa={"block_preexisting": True}), CAND, ctx).verdict == "FAIL"


def test_unattributed_or_regression_high_finding_fails(tmp_path):
    for bb in ("different", "not-reproduced", "not compared"):
        ctx = good_ctx(tmp_path / bb)
        ctx["workers"]["ios"]["verdict"]["findings"] = [_high(bb)]
        assert evaluate(make_cfg(tmp_path / bb), CAND, ctx).verdict == "FAIL", bb


def test_unselected_platform_is_not_judged(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.data["qa"]["platforms"] = ["ios", "android"]
    cfg.data["qa"]["required_suites"] = ["ios-ui", "android-ui"]
    cfg.data["suites"]["android-ui"] = {"platform": "android", "command": "true", "results": {"kind": "junit", "path": "x"}}
    cfg.data["platforms"]["android"] = {"package": "x"}
    ctx = good_ctx(tmp_path, platforms=["ios"])     # this run selected iOS only
    g = evaluate(cfg, CAND, ctx)
    assert g.verdict == "PASS", [c for c in g.checks if not c.ok]
    assert any(c.name == "suite.android-ui" and "not selected" in c.detail for c in g.checks)
