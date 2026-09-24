"""Render the run report (Markdown + JSON) from durable run state. Redacts obvious secrets."""

from __future__ import annotations

import json
import platform as _platform
from pathlib import Path

from .gate import GateResult, sev_rank
from .util import redact, human_duration, run as _run, write_json


def toolchain_facts() -> dict:
    facts = {"host_os": f"macOS {_platform.mac_ver()[0]}" if _platform.system() == "Darwin" else _platform.platform(),
             "arch": _platform.machine()}
    r = _run(["xcodebuild", "-version"], timeout=30)
    if r.ok:
        facts["xcode"] = " ".join(r.stdout.split()[:4])
    return facts


def render_markdown(run_state: dict, cand: dict, cfg_data: dict, ctx: dict, gate: GateResult, run_dir: Path) -> str:
    L: list[str] = []
    v = gate.verdict
    badge = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "BLOCKED": "⛔ BLOCKED", "CANCELLED": "🛑 CANCELLED"}.get(v, v)
    L.append(f"# CodeAndConfirm report — {badge}")
    L.append("")
    if ctx.get("perf_only"):
        L.append("> **Benchmark-only run.** No functional QA was performed and no approval was recorded; the verdict covers performance measurements only.")
        L.append("")
    backend = ctx.get("backend") or {}
    if backend.get("kind") == "firebase-real":
        d = backend.get("detail") or {}
        cl = ctx.get("backend_cleanup")
        if not (cl or {}).get("ok"):
            why = (cl or {}).get("error") or "the cleanup step never ran"
            L.append(f"> ⚠️ **NEEDS CLEANUP.** Real project `{d.get('project_id')}` may still hold accounts `{d.get('account_prefix')}*` and their data: "
                     f"{redact(str(why))}. Retry with `codeandconfirm cleanup {run_state['run_id']}`.")
            L.append("")
    L.append(f"- **Run:** `{run_state['run_id']}`  ·  started {run_state.get('created_at')}  ·  repair cycle {run_state.get('repair_cycle', 0)}"
             + (f"  ·  profile **{run_state.get('profile')}**" if run_state.get("profile") else ""))
    L.append(f"- **Candidate:** `{cand.get('candidate_sha','')[:12]}` ({cand.get('candidate_ref')}) tested as **{cand.get('tested_ref_kind','head')}**")
    L.append(f"- **Base:** `{cand.get('base_sha','')[:12]}` ({cand.get('base_ref')}) · merge-base `{cand.get('merge_base_sha','')[:12]}`")
    if cand.get("pr"):
        pr = cand["pr"]
        L.append(f"- **PR:** [{pr.get('repo')}#{pr.get('number')}]({pr.get('url')}) by {pr.get('author')} · fork={pr.get('is_fork')} · trust={cand.get('trust')}")
    models = sorted({m for w in (ctx.get("workers") or {}).values() for m in (w.get("model_used") or [])})
    roles = ctx.get("roles") or {"lead": (cfg_data.get("roles") or {}).get("lead", "claude"), "qa": (cfg_data.get("roles") or {}).get("qa", "codex")}
    engine = roles.get("qa", "codex")
    requested = (cfg_data.get("claude") or {}).get("model") if engine == "claude" else (cfg_data.get("codex") or {}).get("model")
    L.append(f"- **Roles:** lead `{roles.get('lead')}` · QA `{engine}`" + (" ⚠️ same vendor builds and tests" if roles.get("lead") == engine else ""))
    L.append(f"- **QA model:** requested `{requested}` · verified from session {'transcripts' if engine == 'claude' else 'rollouts'}: `{', '.join(models) or 'none'}`"
             + (f" · {engine} cli {next((w.get('cli_version') for w in (ctx.get('workers') or {}).values() if w.get('cli_version')), '?')}" if models else ""))
    tf = ctx.get("toolchain") or {}
    if backend.get("kind") == "firebase-real":
        d = backend.get("detail") or {}
        backend_desc = f"backend: real project {d.get('project_id')} (firebase-real, environment {d.get('env_name', 'dev')}; accounts {d.get('account_prefix')}*)"
    else:
        backend_desc = f"backend: {backend.get('kind', 'none')} ports {backend.get('ports', {})}"
    L.append(f"- **Host/toolchain:** {tf.get('host_os','?')} {tf.get('arch','')} · {tf.get('xcode','')} · {backend_desc}")
    devs = ctx.get("devices") or {}
    if devs:
        L.append("- **Devices:** " + " · ".join(f"{p}: `{d.get('id')}` {d.get('name','')} {d.get('os','')}".strip() for p, d in devs.items()))
    sel = ctx.get("platform_selection") or {}
    if sel.get("mode") in ("diff", "diff-fallback"):
        L.append(f"- **Platforms:** {', '.join(sel.get('selected') or []) or 'none'} · {sel.get('reason', '')}")
    if cand.get("acceptance_criteria"):
        L.append("")
        L.append("## Acceptance criteria")
        L.append("")
        L.append(redact(cand["acceptance_criteria"]))
    L.append("")
    L.append("## Gate checks")
    L.append("")
    L.append("| Check | Result | Detail |")
    L.append("|---|---|---|")
    for c in gate.checks:
        mark = "✅" if c.ok else {"fail": "❌", "blocked": "⛔"}.get(c.outcome, "⚠️")
        req = "" if c.required else " (advisory)"
        L.append(f"| `{c.name}`{req} | {mark} | {redact(c.detail).replace('|', '/')[:220]} |")
    if gate.fail_reasons or gate.block_reasons:
        L.append("")
        L.append("**Why not PASS:**")
        for r in gate.fail_reasons:
            L.append(f"- ❌ {redact(r)}")
        for r in gate.block_reasons:
            L.append(f"- ⛔ {redact(r)}")

    # suites
    suites = ctx.get("suites") or {}
    if suites:
        L.append("")
        L.append("## Native suites (run by the coordinator)")
        L.append("")
        L.append("| Suite | Exit | Tests | Passed | Failed | Skipped | Duration | Log |")
        L.append("|---|---|---|---|---|---|---|---|")
        for name, s in suites.items():
            r = s.get("result", {})
            L.append(f"| {name} | {s.get('exit_code')}{' (timeout)' if s.get('timed_out') else ''} | {r.get('total')} | {r.get('passed')} | {r.get('failed')} | {r.get('skipped')} | {human_duration(s.get('duration_s', 0))} | `{Path(s.get('log_file','')).name}` |")
            for f in r.get("failed_tests", [])[:10]:
                L.append(f"|  | | | | ↳ `{f}` | | | |")

    # builds
    builds = ctx.get("builds") or {}
    if builds:
        L.append("")
        L.append("## Builds")
        L.append("")
        for p, b in builds.items():
            ident = b.get("identity") or {}
            L.append(f"- **{p}:** {'ok' if b.get('ok') else 'FAILED'} exit={b.get('exit_code')} in {human_duration(b.get('duration_s', 0))} · "
                     f"artifact `{Path(b.get('artifact') or '').name}` · sha256 `{str(ident.get('sha256',''))[:16]}` · version {ident.get('version')}")
            if b.get("error"):
                L.append(f"  - error: `{redact(b['error'])[:300]}`")

    # workers
    workers = ctx.get("workers") or {}
    if workers:
        L.append("")
        L.append("## QA workers")
        for name, w in workers.items():
            v = w.get("verdict") or {}
            L.append("")
            L.append(f"### {name} — {v.get('verdict', 'no verdict')}")
            L.append("")
            L.append(f"exit={w.get('exit_code')} timed_out={w.get('timed_out')} commands={w.get('commands')} tool_calls={w.get('mcp_calls')} "
                     f"tokens in/out={w.get('usage', {}).get('input_tokens')}/{w.get('usage', {}).get('output_tokens')} "
                     f"(cached in {w.get('usage', {}).get('cached_input_tokens')}, reasoning out {w.get('usage', {}).get('reasoning_output_tokens')}) thread=`{w.get('thread_id')}`")
            if v.get("summary"):
                L.append("")
                L.append(redact(v["summary"]))
            if w.get("errors"):
                L.append("")
                L.append("Worker errors: " + "; ".join(redact(e)[:200] for e in w["errors"][:3]))
            if v.get("untested"):
                L.append("")
                L.append("Untested / limitations:")
                for u in v["untested"]:
                    L.append(f"- {redact(u)}")
            if v.get("blockers"):
                L.append("")
                L.append("Blockers:")
                for u in v["blockers"]:
                    L.append(f"- {redact(u)}")
            if v.get("test_patches"):
                L.append("")
                L.append("Proposed test patches (not applied): " + ", ".join(f"`{t}`" for t in v["test_patches"]))

    # scenarios
    if gate.scenarios:
        L.append("")
        L.append("## Scenarios")
        L.append("")
        L.append("| Worker | Id | Status | Evidence | Notes |")
        L.append("|---|---|---|---|---|")
        for s in gate.scenarios:
            ev = ", ".join(f"`{e}`" for e in (s.get("evidence") or [])[:4])
            L.append(f"| {s.get('worker')} | {s.get('id')} | {s.get('status')} | {ev} | {redact(s.get('notes',''))[:160].replace('|','/')} |")

    # findings
    if gate.all_findings:
        L.append("")
        L.append("## Findings")
        for f in sorted(gate.all_findings, key=lambda x: -sev_rank(x.get("severity", "info"))):
            L.append("")
            L.append(f"### [{f.get('severity','?').upper()}] {redact(f.get('title',''))}  ({f.get('category')}, {f.get('platform')}, from {f.get('worker')})")
            L.append("")
            L.append(redact(f.get("description", "")))
            if f.get("reproduction"):
                L.append("")
                L.append("Reproduction:")
                for i, step in enumerate(f["reproduction"], 1):
                    L.append(f"{i}. {redact(step)}")
            if f.get("evidence"):
                L.append("")
                L.append("Evidence: " + ", ".join(f"`{e}`" for e in f["evidence"]))
            if f.get("compared_to_base"):
                L.append("")
                L.append(f"Compared to base: {redact(f['compared_to_base'])}")
            if f.get("suggested_regression_test"):
                L.append("")
                L.append(f"Suggested regression test: {redact(f['suggested_regression_test'])}")

    perf = ctx.get("perf")
    if perf:
        L.append("")
        L.append("## Performance (preliminary: simulator/emulator)")
        L.append("")
        L.append(redact(perf.get("summary", "")))
        for row in perf.get("table", []):
            L.append(f"- {row}")
        if perf.get("regressions"):
            L.append("")
            L.append("Regressions: " + "; ".join(perf["regressions"]))
        if perf.get("inconclusive"):
            L.append("")
            L.append("Inconclusive: " + "; ".join(perf["inconclusive"]))

    cl = ctx.get("backend_cleanup")
    if backend.get("kind") == "firebase-real":
        L.append("")
        L.append("## Real backend cleanup")
        L.append("")
        if cl:
            L.append(f"- project `{cl.get('project_id')}` · prefix `{cl.get('account_prefix')}` · ok={cl.get('ok')} · accounts removed {cl.get('deleted_users')} · "
                     f"trees removed {cl.get('deleted_trees')} · remaining {cl.get('remaining') if cl.get('verified') else 'not verified'} · log `{Path(cl.get('log_file') or '').name}`")
            if cl.get("error"):
                L.append(f"- error: {redact(str(cl['error']))}")
        else:
            L.append("- cleanup did not run")
    L.append("")
    L.append("## Artifacts")
    L.append("")
    L.append(f"- Run directory: `{run_dir}`")
    L.append(f"- Evidence: `{run_dir / 'evidence'}` · Codex workspaces: `{run_dir / 'codex'}` · command log: `{run_dir / 'artifacts' / 'commands.jsonl'}`")
    L.append("")
    L.append("_Generated by CodeAndConfirm, an independent community project (not affiliated with Anthropic or OpenAI)._")
    return "\n".join(L) + "\n"


def write_report(run_dir: Path, run_state: dict, cand: dict, cfg_data: dict, ctx: dict, gate: GateResult) -> tuple[Path, Path]:
    md = render_markdown(run_state, cand, cfg_data, ctx, gate, run_dir)
    (run_dir / "report.md").write_text(md)
    data = {"run": run_state, "candidate": cand, "gate": gate.to_dict(), "context": _json_safe(ctx)}
    write_json(run_dir / "report.json", data)
    return run_dir / "report.md", run_dir / "report.json"


def _json_safe(obj):
    try:
        json.dumps(obj, default=str)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))


def short_summary(gate: GateResult, cand: dict, run_id: str) -> str:
    lines = [f"CodeAndConfirm {gate.verdict} — run {run_id} — candidate {cand.get('candidate_sha','')[:10]} vs base {cand.get('base_sha','')[:10]}"]
    bad = [c for c in gate.checks if not c.ok and c.required]
    for c in bad[:8]:
        lines.append(f"  - {c.name}: {c.detail[:160]}")
    if gate.blocking_findings:
        for f in gate.blocking_findings[:5]:
            lines.append(f"  - finding [{f.get('severity')}] {f.get('title')}")
    return "\n".join(lines)
