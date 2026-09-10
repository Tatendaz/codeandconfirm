"""The gate: independent validation of evidence. A worker's narrative alone never yields PASS.

Approval records live under $CODEANDCONFIRM_HOME/approvals/<repo-key>/<candidate-sha>.json —
outside the candidate's editable files. They are bound to the exact candidate SHA, base SHA,
tested-ref kind, configuration fingerprint and required plan. They are locally writable files:
they are not tamper-proof, which is why docs recommend a server-side status check as the merge
backstop.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import Config, home_dir
from .util import now_iso, read_json, read_jsonl, write_json

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]


def sev_rank(s: str) -> int:
    return SEVERITY_ORDER.index(s) if s in SEVERITY_ORDER else 0


@dataclass
class Check:
    name: str
    ok: bool
    category: str          # integrity|model|infra|suite|evidence|journey|finding|perf|worker
    detail: str = ""
    required: bool = True
    outcome: str = "ok"    # ok|fail|blocked|info


@dataclass
class GateResult:
    verdict: str
    checks: list[Check] = field(default_factory=list)
    fail_reasons: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    blocking_findings: list[dict] = field(default_factory=list)
    all_findings: list[dict] = field(default_factory=list)
    scenarios: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def config_fingerprint(cfg: Config) -> str:
    rel = {k: cfg.data.get(k) for k in ("qa", "suites", "journeys", "platforms", "perf", "backend", "codex", "roles", "claude")}
    return hashlib.sha256(json.dumps(rel, sort_keys=True, default=str).encode()).hexdigest()[:16]


def repo_key(repo_root: str | Path) -> str:
    return hashlib.sha256(str(Path(repo_root).resolve()).encode()).hexdigest()[:16]


_TEST_PATH_HINTS = ("uitests", "/test/", "/tests/", "androidtest", "__tests__", "/spec/")
# Operations tooling next to product code: sweep/seed scripts, per-environment configs, CI — not behaviour a
# device worker can exercise, so a change there asks for no diff-derived scenario.
_OPS_PATH_HINTS = ("/scripts/", "/config/", "/.github/", ".github/", "/seed/", "/seed-bulk/")
_NON_CODE_SUFFIXES = (".md", ".txt", ".png", ".jpg", ".toml", ".yml", ".yaml", ".lock", ".json")


def platform_touched(changed_files: list[str], platform: str) -> bool:
    """Does the diff change product code that runs on `platform`? Test-only, docs-only, config and operations
    tooling changes do not count; shared backend code (rules, functions, mock server) counts for every platform."""
    for f in changed_files or []:
        fl = f.lower()
        if any(h in fl for h in _TEST_PATH_HINTS) or any(h in fl for h in _OPS_PATH_HINTS) or fl.endswith(_NON_CODE_SUFFIXES):
            continue
        if platform == "ios" and (fl.startswith("ios/") or fl.endswith(".swift")):
            return True
        if platform == "android" and (fl.startswith("android/") or fl.endswith((".kt", ".java"))):
            return True
        if fl.startswith(("firebase/", "mock-server/", "backend/", "server/")):
            return True
    return False


def qa_engine(cfg: Config) -> str:
    return str(cfg.get("roles.qa", "codex") or "codex").lower()


def qa_model_requested(cfg: Config) -> str:
    return str(cfg.get("claude.model") if qa_engine(cfg) == "claude" else cfg.get("codex.model"))


def _model_ok(requested: str, used: str) -> bool:
    # Codex ids are exact. Claude ids may come back as a dated variant of the requested id.
    return used == requested or used.startswith(requested + "-")


def _evidence_exists(evidence_dir: Path, ref: str) -> bool:
    if not ref:
        return False
    p = Path(ref)
    cands = [p] if p.is_absolute() else [evidence_dir / ref, evidence_dir / Path(ref).name]
    return any(c.exists() and c.is_file() and c.stat().st_size > 0 for c in cands)


def evaluate(cfg: Config, cand: dict, ctx: dict) -> GateResult:
    """ctx: platform_status, builds, installs, proofs, suites, workers, perf, checkout_head_start,
    checkout_head_end, pristine, cancelled, run_started_at, evidence_root, worker_evidence."""
    g = GateResult("PASS")
    fail, block = g.fail_reasons, g.block_reasons
    qa = cfg.data["qa"]
    platforms = list(ctx.get("platforms") or qa.get("platforms", []))   # the platforms THIS run selected
    block_sev = qa.get("block_severity", "high")
    block_preexisting = bool(qa.get("block_preexisting", False))

    def add(name, ok, cat, detail="", required=True, outcome=None):
        oc = outcome or ("ok" if ok else ("fail" if cat in ("integrity", "suite", "finding", "perf") else "blocked"))
        g.checks.append(Check(name, ok, cat, detail, required, oc))
        if not ok and required:
            (fail if oc == "fail" else block).append(f"{name}: {detail}")

    if ctx.get("cancelled"):
        g.verdict = "CANCELLED"
        add("run.cancelled", False, "infra", "run was cancelled", outcome="blocked")
        return g

    # --- integrity ---------------------------------------------------------------
    hs, he = ctx.get("checkout_head_start"), ctx.get("checkout_head_end")
    add("candidate.sha-pinned", hs == cand.get("candidate_sha") == he,
        "integrity", f"start={str(hs)[:10]} end={str(he)[:10]} expected={str(cand.get('candidate_sha'))[:10]}")
    pristine, offending = ctx.get("pristine", (True, []))
    add("candidate.unmodified", bool(pristine), "integrity",
        "checkout has no tracked changes" if pristine else f"tracked files changed during QA: {offending[:8]}")

    # --- builds ------------------------------------------------------------------
    for p in platforms:
        b = (ctx.get("builds") or {}).get(p)
        st = (ctx.get("platform_status") or {}).get(p) or {}
        if st.get("blocked"):
            add(f"platform.{p}.available", False, "infra", st["blocked"], outcome="blocked")
            continue
        if b is None:
            add(f"build.{p}", False, "infra", "no build attempted", outcome="blocked")
        else:
            add(f"build.{p}", bool(b.get("ok")), "integrity" if not b.get("ok") else "infra",
                f"exit={b.get('exit_code')} {b.get('error','')[:200]}".strip(), outcome=None if b.get("ok") else "fail")

    if ctx.get("perf_only"):
        _evaluate_perf_only(g, cfg, ctx, add)
        g.verdict = "FAIL" if fail else ("BLOCKED" if block else "PASS")
        g.summary = {"mode": "benchmark-only (no functional QA; not an approval)", "checks": len(g.checks)}
        return g

    # --- installs / identity / proofs ---------------------------------------------
    for p in platforms:
        st = (ctx.get("platform_status") or {}).get(p) or {}
        if st.get("blocked"):
            continue
        inst = (ctx.get("installs") or {}).get(p)
        add(f"install.{p}.identity", bool(inst and inst.get("identity_match")), "evidence",
            "installed build hash matches built artifact" if inst and inst.get("identity_match") else "installed build identity not verified")
        pr = (ctx.get("proofs") or {}).get(p)
        add(f"adapter.{p}.proof", bool(pr and pr.get("ok")), "infra",
            f"{len((pr or {}).get('steps', []))} steps" if pr and pr.get("ok") else (pr or {}).get("error", "adapter proof missing"))

    # --- suites --------------------------------------------------------------------
    required_suites = qa.get("required_suites", [])
    suites = ctx.get("suites") or {}
    for name in required_suites:
        s = suites.get(name)
        plat = (cfg.suites.get(name) or {}).get("platform")
        if plat in ("ios", "android") and plat not in platforms:
            add(f"suite.{name}", True, "suite", f"skipped: platform {plat} not selected for this run", required=False, outcome="info")
            continue
        if plat in platforms and ((ctx.get("platform_status") or {}).get(plat) or {}).get("blocked"):
            add(f"suite.{name}", False, "infra", f"platform {plat} blocked", outcome="blocked")
            continue
        if s is None:
            add(f"suite.{name}", False, "infra", "required suite was not executed", outcome="blocked")
            continue
        res = s.get("result", {})
        if s.get("timed_out"):
            add(f"suite.{name}", False, "infra", f"timed out after {s.get('duration_s')}s", outcome="blocked")
        elif res.get("kind") == "none":
            # no result artifact declared: the recorded exit code is the only signal (documented limitation)
            add(f"suite.{name}", s.get("exit_code") == 0, "suite", f"exit={s.get('exit_code')} (exit code only; declare a results artifact for per-test counts)")
        elif not res.get("parsed") or res.get("total", 0) == 0:
            add(f"suite.{name}", False, "infra", f"exit={s.get('exit_code')}; no parseable test results ({res.get('detail')})", outcome="blocked")
        elif res.get("failed", 0) or res.get("errors", 0) or s.get("exit_code") != 0:
            add(f"suite.{name}", False, "suite", f"exit={s.get('exit_code')} failed={res.get('failed')} errors={res.get('errors')} of {res.get('total')}: {res.get('failed_tests', [])[:5]}")
        else:
            flaky = s.get("flaky_history") or []
            add(f"suite.{name}", True, "suite", f"{res.get('passed')}/{res.get('total')} passed" + (f" (after {len(flaky)} infra retry)" if flaky else ""))

    # --- workers --------------------------------------------------------------------
    model_req = qa_model_requested(cfg)
    workers = ctx.get("workers") or {}
    expected_workers = (["review"] if qa.get("static_review", True) else []) + \
        [p for p in platforms if not ((ctx.get("platform_status") or {}).get(p) or {}).get("blocked")]
    for w in expected_workers:
        r = workers.get(w)
        if r is None:
            add(f"worker.{w}.completed", False, "worker", "worker never ran", outcome="blocked")
            continue
        if r.get("cancelled"):
            add(f"worker.{w}.completed", False, "worker", "cancelled", outcome="blocked"); continue
        if r.get("timed_out"):
            add(f"worker.{w}.completed", False, "worker", "timed out before producing a verdict", outcome="blocked"); continue
        if r.get("exit_code") != 0:
            add(f"worker.{w}.completed", False, "worker", f"codex exited {r.get('exit_code')}: {(r.get('errors') or [''])[-1][:200]}", outcome="blocked"); continue
        v = r.get("verdict")
        if not v or v.get("verdict") not in ("PASS", "FAIL", "BLOCKED"):
            add(f"worker.{w}.verdict-parsed", False, "worker", r.get("verdict_error") or "no structured verdict", outcome="blocked"); continue
        add(f"worker.{w}.completed", True, "worker", f"{r.get('commands', 0)} commands, {r.get('mcp_calls', 0)} tool calls")
        used = r.get("model_used") or []
        if not used:
            add(f"worker.{w}.model-verified", False, "model", "no session rollout found to verify the model", outcome="blocked")
        elif any(not _model_ok(model_req, m) for m in used):
            add(f"worker.{w}.model-verified", False, "model", f"requested {model_req} but session used {used}", outcome="blocked")
        else:
            add(f"worker.{w}.model-verified", True, "model", f"{used[0]} (cli {r.get('cli_version')})")
        wv = v.get("verdict")
        if wv == "FAIL":
            blocking_here = [f for f in (v.get("findings") or []) if sev_rank(f.get("severity", "info")) >= sev_rank(block_sev)]
            failed_journeys = [s for s in (v.get("scenarios") or []) if s.get("status") == "failed" and s.get("id") in (qa.get("required_journeys") or [])]
            if blocking_here or failed_journeys:
                add(f"worker.{w}.verdict", False, "finding", "worker verdict FAIL", outcome="fail")
            else:
                # Policy, not narrative: only findings at/above block_severity (or failed required journeys) fail
                # the run. The worker's FAIL is kept visible as an advisory check with its findings listed.
                add(f"worker.{w}.verdict", False, "finding",
                    f"worker said FAIL, but its findings are below block_severity={block_sev} and no required journey failed; "
                    f"reported, not blocking: " + "; ".join(f"[{f.get('severity')}] {f.get('title')}" for f in (v.get("findings") or [])[:4]),
                    required=False, outcome="info")
        elif wv == "BLOCKED":
            add(f"worker.{w}.verdict", False, "worker", f"worker verdict BLOCKED: {'; '.join(v.get('blockers', [])[:3])}", outcome="blocked")
        else:
            add(f"worker.{w}.verdict", True, "worker", "worker verdict PASS")
        for sc in v.get("scenarios", []) or []:
            g.scenarios.append({**sc, "worker": w})
        for f in v.get("findings", []) or []:
            g.all_findings.append({**f, "worker": w})

    # --- hands-on evidence per platform -----------------------------------------------
    min_inter = int(qa.get("min_interactions_per_platform", 6))
    ev_root = Path(ctx.get("evidence_root", "."))
    for p in platforms:
        if ((ctx.get("platform_status") or {}).get(p) or {}).get("blocked"):
            continue
        wdir = ev_root / p
        recs = read_jsonl(wdir / "actions.jsonl")
        worker_started = (workers.get(p) or {}).get("started_at") or ctx.get("run_started_at") or ""
        after = [r for r in recs if r.get("ts", "") >= worker_started]   # the adapter proof's own taps do not count
        inter = [r for r in after if r.get("action") in ("tap", "type", "key", "back", "home", "scroll", "swipe", "dismiss-keyboard") and r.get("ok", True)]
        shots = [f for f in wdir.glob("*.png") if f.stat().st_size > 0 and any(r.get("file") == f.name for r in after if r.get("action") == "screenshot")]
        started = ctx.get("run_started_at") or ""
        in_window = all(r.get("ts", "") >= started for r in recs) if recs else False
        ok = len(inter) >= min_inter and len(shots) >= 1 and in_window
        add(f"evidence.{p}.hands-on", ok, "evidence",
            f"{len(inter)} interactions (min {min_inter}), {len(shots)} screenshots" + ("" if in_window else ", evidence timestamps precede run start"))
        # the worker must have verified it was driving the assigned build
        ident = read_json(wdir / "build-identity.json", {}) or read_json(ev_root / f"{p}-install" / "build-identity.json", {})
        add(f"evidence.{p}.build-identity", bool(ident.get("identity_match")), "evidence",
            "assigned build verified on device" if ident.get("identity_match") else "build identity record missing or mismatched")
        fin = (ctx.get("final_identity") or {}).get(p)
        if fin is not None:
            add(f"evidence.{p}.final-build", bool(fin.get("match")), "evidence",
                "candidate build still installed when QA ended" if fin.get("match") else f"device ended on a different build ({fin.get('installed', '?')[:12]}); a base build left installed invalidates the evidence")

    # --- required journeys -----------------------------------------------------------------
    req_j = list(qa.get("required_journeys", []) or cfg.get("journeys.required", []))
    for jid in req_j:
        for p in platforms:
            if ((ctx.get("platform_status") or {}).get(p) or {}).get("blocked"):
                add(f"journey.{jid}.{p}", False, "infra", f"platform {p} blocked", outcome="blocked"); continue
            hits = [s for s in g.scenarios if s.get("worker") == p and s.get("id") == jid]
            if not hits:
                add(f"journey.{jid}.{p}", False, "journey", "not reported by the platform worker", outcome="blocked"); continue
            s = hits[-1]
            evs = [e for e in (s.get("evidence") or []) if _evidence_exists(ev_root / p, e)]
            if s.get("status") == "passed":
                add(f"journey.{jid}.{p}", bool(evs), "journey", f"passed with {len(evs)} evidence file(s)" if evs else "marked passed but cites no existing evidence file", outcome=None if evs else "blocked")
            elif s.get("status") == "failed":
                add(f"journey.{jid}.{p}", False, "journey", f"failed: {s.get('notes','')[:160]}", outcome="fail")
            else:
                add(f"journey.{jid}.{p}", False, "journey", f"{s.get('status')}: {s.get('notes','')[:160]}", outcome="blocked")

    # --- diff-derived scenarios (pr profile) ------------------------------------------------------
    if qa.get("require_diff_scenarios"):
        for p in platforms:
            if ((ctx.get("platform_status") or {}).get(p) or {}).get("blocked"):
                continue
            hits = [s for s in g.scenarios if s.get("worker") == p and str(s.get("id", "")).startswith("diff.") and s.get("status") in ("passed", "failed")]
            if platform_touched(cand.get("changed_files") or [], p):
                add(f"evidence.{p}.diff-scenarios", bool(hits), "journey",
                    f"{len(hits)} diff-derived scenario(s) reported" if hits else "the diff touches this platform but the worker reported no diff.* scenario",
                    outcome=None if hits else "blocked")
            else:
                add(f"evidence.{p}.diff-scenarios", True, "journey", f"{len(hits)} diff-derived scenario(s); the diff changes no {p} product code", required=False, outcome="info")

    # --- findings ----------------------------------------------------------------------------
    thr = sev_rank(block_sev)
    preexisting = []
    for f in g.all_findings:
        if sev_rank(f.get("severity", "info")) >= thr:
            concrete = bool(f.get("reproduction")) and any(_evidence_exists(ev_root / f.get("worker", ""), e) for e in (f.get("evidence") or []))
            f["concrete"] = concrete
            if f.get("base_behavior") == "same" and not block_preexisting:
                f["classification"] = "pre-existing"      # the base build has the identical defect: not this candidate's regression
                preexisting.append(f)
            else:
                f["classification"] = "regression" if f.get("base_behavior") == "different" else "unattributed"
                g.blocking_findings.append(f)
    if g.blocking_findings:
        titles = "; ".join(f"[{f['severity']}/{f.get('classification')}] {f['title']}" for f in g.blocking_findings[:5])
        add("findings.blocking", False, "finding", f"{len(g.blocking_findings)} finding(s) at/above {block_sev}: {titles}", outcome="fail")
    else:
        add("findings.blocking", True, "finding", f"no blocking findings at/above {block_sev}")
    if preexisting:
        add("findings.pre-existing", True, "finding", f"{len(preexisting)} finding(s) at/above {block_sev} reproduced identically on the base build; reported, not blocking "
            f"(set qa.block_preexisting = true to block): " + "; ".join(f"[{f['severity']}] {f['title']}" for f in preexisting[:4]), required=False, outcome="info")

    # --- platform selection (qa.platforms_from_diff) ----------------------------------------------
    # A platform the diff does not touch was not tested at all; the checks table says so rather than the
    # report implying two-platform coverage.
    sel = ctx.get("platform_selection") or {}
    if sel.get("skipped"):
        add("platforms.not-tested", True, "infra", f"{', '.join(sel['skipped'])} not tested this run: {sel.get('reason') or 'not selected'}",
            required=False, outcome="info")

    # --- real backend: account cleanup ------------------------------------------------------------
    # A failed sweep never changes the product verdict (it is our housekeeping, not the candidate's defect),
    # but it is a required-visibility flag: the report banner, `status` and a durable marker all shout it.
    backend = ctx.get("backend") or {}
    if backend.get("kind") == "firebase-real":
        d = backend.get("detail") or {}
        add("backend.real-project", True, "infra", f"real project {d.get('project_id')} (environment {d.get('env_name', 'dev')}), "
            f"accounts {d.get('account_prefix')}*", required=False, outcome="info")
        cl = ctx.get("backend_cleanup")
        if cl is None:
            add("backend.cleanup", False, "infra", "cleanup never ran: accounts from this run may remain in the real project (needs cleanup)",
                required=False, outcome="fail")
        elif cl.get("ok"):
            add("backend.cleanup", True, "infra", f"{cl.get('deleted_users')} account(s), {cl.get('deleted_trees')} tree(s) removed; "
                f"{'verified 0 remaining' if cl.get('verified') else 'not independently verified'}", required=False)
        else:
            add("backend.cleanup", False, "infra", f"NEEDS CLEANUP: {cl.get('error')}", required=False, outcome="fail")

    # --- performance ---------------------------------------------------------------------------
    perf = ctx.get("perf")
    if cfg.get("perf.enabled"):
        if not perf:
            add("perf.measured", False, "perf", "performance phase did not run", required=bool(cfg.get("perf.required", False)), outcome="blocked")
        else:
            if perf.get("regressions"):
                add("perf.no-regression", False, "perf", "; ".join(perf["regressions"])[:300], outcome="fail")
            elif perf.get("inconclusive"):
                add("perf.conclusive", False, "perf", "; ".join(perf["inconclusive"])[:300], required=bool(cfg.get("perf.required", False)), outcome="blocked")
            else:
                add("perf.no-regression", True, "perf", perf.get("summary", "within thresholds"))

    g.verdict = "FAIL" if fail else ("BLOCKED" if block else "PASS")
    g.summary = {"checks": len(g.checks), "failed_checks": sum(1 for c in g.checks if not c.ok and c.required),
                 "findings": len(g.all_findings), "blocking_findings": len(g.blocking_findings), "scenarios": len(g.scenarios)}
    return g


def _evaluate_perf_only(g: GateResult, cfg: Config, ctx: dict, add) -> None:
    perf = ctx.get("perf")
    if not perf:
        add("perf.measured", False, "perf", "performance phase did not run", outcome="blocked"); return
    if perf.get("regressions"):
        add("perf.no-regression", False, "perf", "; ".join(perf["regressions"])[:300], outcome="fail")
    if perf.get("inconclusive"):
        add("perf.conclusive", False, "perf", "; ".join(perf["inconclusive"])[:300], outcome="blocked")
    if not perf.get("regressions") and not perf.get("inconclusive"):
        add("perf.no-regression", True, "perf", perf.get("summary", "within thresholds"))


# --- approvals ------------------------------------------------------------------------------------


def approvals_dir(repo_root: str | Path) -> Path:
    d = home_dir() / "approvals" / repo_key(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def approve(run_id: str, repo_root: str, cand: dict, cfg: Config, gate: GateResult, extra: dict | None = None) -> Path:
    rec = {
        "run_id": run_id, "verdict": gate.verdict, "candidate_sha": cand["candidate_sha"], "base_sha": cand["base_sha"],
        "candidate_ref": cand.get("candidate_ref"), "base_ref": cand.get("base_ref"), "tested_ref_kind": cand.get("tested_ref_kind"),
        "config_fingerprint": config_fingerprint(cfg), "platforms": cfg.get("qa.platforms"), "profile": cfg.get("profile.name") or None,
        "required_suites": cfg.get("qa.required_suites"), "required_journeys": cfg.get("qa.required_journeys"),
        "created_at": now_iso(), "note": "locally writable record; not tamper-proof. Use the server-side status check as the merge backstop.",
        **(extra or {}),
    }
    p = approvals_dir(repo_root) / f"{cand['candidate_sha']}.json"
    write_json(p, rec)
    return p


def check_approval(repo_root: str | Path, sha: str, cfg: Config | None = None, base_sha: str | None = None) -> dict:
    p = approvals_dir(repo_root) / f"{sha}.json"
    rec = read_json(p, None)
    if not rec:
        return {"approved": False, "reason": f"no passing CodeAndConfirm verdict recorded for {sha[:10]}", "path": str(p)}
    if rec.get("verdict") != "PASS":
        return {"approved": False, "reason": f"recorded verdict is {rec.get('verdict')}", "record": rec}
    if cfg is not None and rec.get("config_fingerprint") != config_fingerprint(cfg):
        return {"approved": False, "reason": "configuration changed since the verdict (required plan differs)", "record": rec}
    if base_sha and rec.get("base_sha") != base_sha:
        return {"approved": True, "warning": f"base moved since QA ({rec.get('base_sha','')[:10]} → {base_sha[:10]}); consider re-running", "record": rec}
    return {"approved": True, "record": rec}


def invalidate_other_shas(repo_root: str | Path, keep_sha: str) -> None:
    """Approvals are per-SHA already; nothing to invalidate. Kept for API symmetry/documentation."""
    return None
