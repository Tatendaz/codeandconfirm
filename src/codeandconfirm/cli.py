"""`codeandconfirm` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, EXAMPLE_CONFIG, EXAMPLE_LOCAL_CONFIG, LOCAL_CONFIG_NAME, REPO_CONFIG_NAME, find_repo_root, home_dir
from .util import eprint, run, which, read_json


def _cfg(ns) -> tuple[Config, Path]:
    repo = Path(ns.repo).resolve() if getattr(ns, "repo", None) else find_repo_root()
    if repo is None:
        repo = Path.cwd()
    explicit = Path(ns.config).resolve() if getattr(ns, "config", None) else None
    try:
        return Config.load(repo_root=repo, explicit=explicit, profile=getattr(ns, "profile", None)), repo
    except ValueError as e:      # unknown profile
        eprint(str(e)); sys.exit(2)


# ----------------------------------------------------------------------------- doctor

def cmd_doctor(ns) -> int:
    from .codex_worker import auth_status, inherited_instruction_risks, resolve_codex
    from .device.android_adb import list_devices, sdk_root
    from .device.base import DeviceError
    from .device.ios_idb import list_simulators
    from .scheduler import host_metrics, ports_in_use, functional_slots
    from .target import tool_env

    cfg, repo = _cfg(ns)
    rows: list[tuple[str, bool | None, str]] = []   # (name, ok/None=warn, detail)

    def row(name, ok, detail=""):
        rows.append((name, ok, detail))

    row("python", sys.version_info >= (3, 11), sys.version.split()[0])
    row("config", bool(cfg.sources), ", ".join(str(s) for s in cfg.sources) or f"no {REPO_CONFIG_NAME} found (run `codeandconfirm init`)")
    prof = cfg.get("profile.name")
    row("config.profile", None, f"{prof} — {cfg.get('profile.description', '')}" if prof else f"none (defined: {sorted(cfg.get('profiles', {}) or [])}); --profile selects one")
    # roles + the QA engine
    lead, qa_role = str(cfg.get("roles.lead", "claude")), str(cfg.get("roles.qa", "codex"))
    row("roles", None if lead == qa_role else True,
        f"lead={lead} qa={qa_role}" + (" — same vendor builds and tests; independence is reduced (set [roles].qa to the other one)" if lead == qa_role else ""))
    if qa_role == "claude":
        from .claude_worker import resolve_claude
        rc2 = resolve_claude(cfg.get("claude.binary", ""), cfg.get("claude.model"))
        row("claude.binary", rc2["ok"], f"{rc2.get('binary')} v{rc2.get('version')}; model {cfg.get('claude.model')} is verified per worker from the session transcript"
            if rc2["ok"] else rc2.get("reason", ""))
    elif qa_role != "codex":
        row("roles.qa", False, f"{qa_role!r} is not supported (codex | claude)")
    # codex
    model = cfg.get("codex.model")
    rc = resolve_codex(cfg.get("codex.binary", ""), model)
    row("codex.binary+model", rc["ok"] if qa_role == "codex" else None, f"{rc.get('binary')} v{rc.get('version')} serves {model}" if rc["ok"]
        else rc.get("reason", "") + " · checked: " + "; ".join(f"{c['binary']} v{c['version']} models={c['models']}" for c in rc.get("checked", [])))
    au = auth_status()
    row("codex.auth", au.get("logged_in") if qa_role == "codex" else None, f"mode={au.get('mode')} last_refresh={au.get('last_refresh')}" if au.get("logged_in") else "run `codex login`")
    for risk in inherited_instruction_risks():
        row("codex.inherited-instructions", None, risk)
    sandbox = cfg.get("codex.sandbox")
    row("codex.sandbox", None, f"{sandbox} — device tools (simctl/adb/idb) do not work inside the Codex seatbelt sandbox; trusted candidates run unsandboxed, forks run read-only static review only")
    # tools
    te = tool_env(cfg)
    row("xcodebuild", bool(which("xcodebuild")), run(["xcodebuild", "-version"], timeout=30).stdout.replace("\n", " ").strip() if which("xcodebuild") else "install Xcode")
    row("simctl", run(["xcrun", "simctl", "help"], timeout=30).ok if which("xcrun") else False, "")
    row("idb", bool(which("idb")) and bool(which("idb_companion")), f"idb={which('idb')} companion={which('idb_companion')}" if which("idb") else
        "brew tap facebook/fb && brew install idb-companion && uv tool install fb-idb --python 3.12")
    try:
        sdk = sdk_root(te["android_sdk"])
        row("android.sdk", True, str(sdk))
        row("android.emulator", (sdk / "emulator" / "emulator").exists(), "")
        avds = run([str(sdk / "emulator" / "emulator"), "-list-avds"], timeout=60).stdout.split()
        wants = cfg.get("platforms.android.avd_names") or [cfg.get("platforms.android.avd_name", "codeandconfirm_api34")]
        row("android.avd", all(w in avds for w in wants), f"{wants} {'present' if all(w in avds for w in wants) else 'missing (init --devices creates it)'}; avds={avds}")
    except DeviceError as e:
        row("android.sdk", False, str(e))
    row("jdk17", bool(te["jdk17_home"]) and Path(te["jdk17_home"]).exists(), te["jdk17_home"] or "set tools.jdk17_home")
    row("jdk21+", bool(te["jdk21_home"]) and Path(te["jdk21_home"]).exists(), te["jdk21_home"] or "set tools.jdk21_home")
    if cfg.get("backend.kind") == "firebase-emulator":
        row("firebase-cli", bool(which("firebase")), run(["firebase", "--version"], timeout=30).stdout.strip() if which("firebase") else "npm i -g firebase-tools")
        ports = list((cfg.get("backend.ports") or {}).values())
        busy = ports_in_use(ports)
        row("backend.ports-free", not busy, f"{ports} busy={busy}" if busy else f"{ports} free")
    if cfg.get("backend.kind") == "firebase-real":
        from .firebase_real import check_credentials
        from .util import expand
        allowed = list(cfg.get("backend.allowed_project_ids") or [])
        suspicious = [a for a in allowed if any(w in a.lower() for w in ("prod", "test", "live", "release"))]
        row("backend.allowed_project_ids", bool(allowed) and not suspicious,
            f"{allowed}" + (f" — names that look like test/prod projects: {suspicious}; CodeAndConfirm is for the development project only" if suspicious else "")
            if allowed else "empty: the real backend refuses to start without an allow-list")
        script = repo / str(cfg.get("backend.env_script", "scripts/use-firebase-env.sh"))
        row("backend.env_script", script.exists(), str(script))
        src = cfg.get("backend.config_source")
        row("backend.config_source", bool(src) and expand(src).is_dir(), f"{src or 'unset'} (machine-local dir with the real client configs; set in {LOCAL_CONFIG_NAME})")
        creds_cfg = cfg.get("backend.cleanup.credentials")
        cp, why = check_credentials(creds_cfg, allowed[0] if allowed else "")
        row("backend.cleanup.credentials", cp is not None, f"{cp} (service account of {allowed[0]})" if cp else (why or "unset"))
        row("backend.cleanup.command", bool(cfg.get("backend.cleanup.command")), "set" if cfg.get("backend.cleanup.command") else "unset: accounts would never be reclaimed")
        row("node", bool(which("node")), which("node") or "install Node.js (the cleanup sweep runs the repo's Admin SDK script)")
        from .firebase_real import list_needs_cleanup
        pending = list_needs_cleanup()
        row("backend.needs-cleanup", not pending, f"{len(pending)} run(s) still need cleanup: {[m.get('run_id') for m in pending]}" if pending else "none pending")
    row("gh", bool(which("gh")) and run(["gh", "auth", "status"], timeout=30).ok, "authenticated" if which("gh") and run(["gh", "auth", "status"], timeout=30).ok else "gh auth login (needed for --pr and --publish)")
    # devices
    sims = list_simulators()
    for want_sim in (cfg.get("platforms.ios.device_names") or [cfg.get("platforms.ios.device_name", "CodeAndConfirm iPhone")]):
        s = next((d for d in sims if d["name"] == want_sim), None)
        row("ios.device", bool(s), f"{want_sim}: {s['state'] if s else 'missing (init --devices creates it)'} {s['udid'] if s else ''}")
    try:
        devs = list_devices(te["android_sdk"])
        row("android.device", None, f"online: {[d['serial'] for d in devs if d['state'] == 'device']} (booted on demand)")
    except DeviceError:
        pass
    row("computer-use", None, "device interaction uses idb/adb (device-scoped, no desktop permission needed). Codex desktop computer-use needs per-app approval in the Codex app and is optional.")
    # host
    m = host_metrics(cfg.get("scheduler.ci_worker_process_pattern", "Runner.Worker"))
    row("host", True, f"{m.cpu_count} cores, {m.mem_total_gb} GB, {m.mem_free_percent}% free, load {m.load_1m}, CI workers busy {m.ci_workers_busy}, functional slots {functional_slots(cfg.data['scheduler'])}")
    free_gb = shutil.disk_usage(home_dir()).free / 1e9
    row("disk", free_gb > 20, f"{free_gb:.0f} GB free at {home_dir()}")
    # required plan sanity
    missing = [s for s in cfg.get("qa.required_suites", []) if s not in cfg.suites]
    row("config.required_suites", not missing, f"missing definitions: {missing}" if missing else f"{cfg.get('qa.required_suites')}")
    cat = cfg.get("journeys.catalog", {}) or {}
    rj = cfg.get("qa.required_journeys", [])
    row("config.required_journeys", all(j in cat for j in rj), f"{rj}" + ("" if all(j in cat for j in rj) else " (some lack catalog descriptions)"))

    worst = 0
    for name, ok, detail in rows:
        mark = "ok  " if ok else ("warn" if ok is None else "FAIL")
        if ok is False:
            worst = 1
        print(f"{mark}  {name:<28} {detail}")
    if ns.json:
        print(json.dumps([{"name": n, "ok": o, "detail": d} for n, o, d in rows], indent=2))
    return worst


# ----------------------------------------------------------------------------- init

def cmd_init(ns) -> int:
    cfg, repo = _cfg(ns)
    wrote = []
    rc = repo / REPO_CONFIG_NAME
    if not rc.exists() or ns.force:
        src = Path(ns.template).read_text() if ns.template else EXAMPLE_CONFIG
        rc.write_text(src); wrote.append(str(rc))
    lc = repo / LOCAL_CONFIG_NAME
    if not lc.exists():
        lc.write_text(EXAMPLE_LOCAL_CONFIG); wrote.append(str(lc))
    gi = repo / ".gitignore"
    if gi.exists() and LOCAL_CONFIG_NAME not in gi.read_text():
        print(f"note: add `{LOCAL_CONFIG_NAME}` to {gi} (machine-specific overrides must not be committed)")
    uc = home_dir() / "config.toml"
    if not uc.exists():
        uc.parent.mkdir(parents=True, exist_ok=True)
        uc.write_text("# CodeAndConfirm machine-wide config (codex binary, tool paths, scheduler)\n" + EXAMPLE_LOCAL_CONFIG)
        wrote.append(str(uc))
    for w in wrote:
        print(f"wrote {w}")
    if ns.devices:
        cfg, repo = _cfg(ns)
        from .device.ios_idb import ensure_simulator
        from .device.android_adb import ensure_avd, boot_emulator
        from .target import tool_env
        te = tool_env(cfg)
        if "ios" in cfg.get("qa.platforms", []):
            for name in (cfg.get("platforms.ios.device_names") or [cfg.get("platforms.ios.device_name", "CodeAndConfirm iPhone")]):
                print(f"ios simulator: {ensure_simulator(name, cfg.get('platforms.ios.device_type', 'iPhone 17'), boot=ns.boot)}")
        if "android" in cfg.get("qa.platforms", []):
            avds = cfg.get("platforms.android.avd_names") or [cfg.get("platforms.android.avd_name", "codeandconfirm_api34")]
            ports = cfg.get("platforms.android.ports") or [int(cfg.get("platforms.android.port", 5580))]
            for avd, port in zip(avds, ports):
                print(f"android avd: {ensure_avd(avd, sdk=te['android_sdk'])}")
                if ns.boot:
                    print(f"android emulator: {boot_emulator(avd, int(port), headless=ns.headless, sdk=te['android_sdk'])}")
    return 0


# ----------------------------------------------------------------------------- review

def cmd_review(ns) -> int:
    from .coordinator import Coordinator, ReviewOptions
    cfg, repo = _cfg(ns)
    if not cfg.sources or not any(s.name in (REPO_CONFIG_NAME,) or s.suffix == ".toml" and s.parent == repo for s in cfg.sources):
        if not ns.config:
            eprint(f"no {REPO_CONFIG_NAME} in {repo}; run `codeandconfirm init` or pass --config")
            return 2
    opts = ReviewOptions(platforms=ns.platforms.split(",") if ns.platforms else None,
                         parallel=(False if ns.no_parallel else None), skip_suites=ns.skip_suites, publish=ns.publish,
                         merge_candidate=ns.merge_candidate, headless_android=ns.headless_android, reset_repairs=ns.reset_repairs,
                         criteria=(Path(ns.criteria_file).read_text() if ns.criteria_file else (ns.criteria or "")),
                         worker_timeout_minutes=ns.worker_timeout, perf_only=bool(getattr(ns, "perf_only", False)))
    co = Coordinator(cfg, repo)
    if ns.pr:
        r = co.start_pr(ns.pr, opts)
    else:
        branch = ns.branch or run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], timeout=15).stdout.strip()
        r = co.start_branch(branch, ns.base, opts)
    print(f"run-id: {r.id}" + (f"  profile: {cfg.get('profile.name')}" if cfg.get("profile.name") else ""))
    if ns.prepare_only:
        print("prepared only (no phases executed); resume with: codeandconfirm resume", r.id)
        return 0
    co.execute(r)
    r.reload()
    print(f"verdict: {r.state.verdict}  report: {r.dir / 'report.md'}")
    if ns.json:
        print(json.dumps({"run_id": r.id, "verdict": r.state.verdict, "reason": r.state.verdict_reason, "report": str(r.dir / 'report.md')}))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 3, "CANCELLED": 4}.get(r.state.verdict or "", 3)


def cmd_status(ns) -> int:
    from .runstore import Run, list_runs
    from .firebase_real import list_needs_cleanup
    if not ns.run_id:
        for st in list_runs(ns.limit):
            print(f"{st.run_id}  {st.status:<9} {st.verdict or '-':<9} phase={st.current_phase:<9} cycle={st.repair_cycle} "
                  f"{(st.profile or '-'):<5} {Path(st.repo_root).name}")
        for m in list_needs_cleanup():
            print(f"NEEDS CLEANUP  run {m.get('run_id')}: real project {m.get('project_id')} may still hold accounts {m.get('account_prefix')}* — "
                  f"{m.get('error')}  → {m.get('retry')}")
        return 0
    r = Run(ns.run_id)
    st = r.state
    cand = r.candidate()
    print(f"run {st.run_id}: status={st.status} verdict={st.verdict or '-'} phase={st.current_phase} coordinator_alive={r.coordinator_alive()} cancel_requested={st.cancel_requested}")
    print(f"candidate {cand.get('candidate_sha','')[:12]} ({cand.get('candidate_ref')}) base {cand.get('base_sha','')[:12]} repair_cycle={st.repair_cycle} model={st.model_requested}→{st.model_used}")
    for name, ph in st.phases.items():
        if ph.get("status") != "pending":
            print(f"  {name:<10} {ph.get('status'):<8} {ph.get('started_at') or ''} → {ph.get('finished_at') or ''} {json.dumps({k: v for k, v in ph.get('detail', {}).items() if k != 'stdout'})[:120]}")
    ctx = read_json(r.dir / "ctx.json", {})
    for p, s in (ctx.get("platform_status") or {}).items():
        if s.get("blocked"):
            print(f"  platform {p}: BLOCKED — {s['blocked']}")
    if (ctx.get("backend") or {}).get("kind") == "firebase-real":
        d = (ctx.get("backend") or {}).get("detail") or {}
        cl = ctx.get("backend_cleanup")
        print(f"  backend: real project {d.get('project_id')} · cleanup {'ok' if (cl or {}).get('ok') else 'NEEDS CLEANUP: ' + str((cl or {}).get('error') or 'did not run')}")
    for w, d in (ctx.get("workers") or {}).items():
        print(f"  worker {w}: exit={d.get('exit_code')} verdict={(d.get('verdict') or {}).get('verdict')} model={d.get('model_used')} thread={d.get('thread_id')}")
    pids = r.pids()
    if pids:
        from .util import pid_alive
        print("  pids: " + ", ".join(f"{k}={v}{'(alive)' if pid_alive(v) else ''}" for k, v in pids.items()))
    if ns.json:
        print(json.dumps({"run": st.__dict__, "candidate": cand, "ctx": ctx}, indent=2, default=str))
    return 0


def cmd_report(ns) -> int:
    from .runstore import Run
    r = Run(ns.run_id)
    p = r.dir / ("report.json" if ns.json else "report.md")
    if not p.exists():
        eprint(f"no report yet for {ns.run_id} (status {r.state.status}, phase {r.state.current_phase})")
        return 1
    print(p.read_text())
    return 0


def cmd_resume(ns) -> int:
    from .coordinator import Coordinator
    from .runstore import Run
    r = Run(ns.run_id)
    if r.state.verdict and not (ns.force or ns.rerun_phase):
        print(f"run {r.id} already finished with {r.state.verdict}; use --force to re-evaluate the gate or --rerun-phase <phase>")
        return 0
    if r.coordinator_alive() and r.state.coordinator_pid != os.getpid():
        eprint(f"coordinator pid {r.state.coordinator_pid} is still running; cancel it first or wait")
        return 2
    from .coordinator import config_for_run
    repo = Path(r.state.repo_root)
    cfg = config_for_run(r)
    if ns.force or ns.rerun_phase:
        r.state.verdict = None; r.state.status = "running"
        r.state.phases["gate"]["status"] = "pending"; r.state.phases["done"]["status"] = "pending"
        for ph in (ns.rerun_phase or []):
            if ph not in r.state.phases:
                eprint(f"unknown phase {ph!r}"); return 2
            r.state.phases[ph]["status"] = "pending"
        r.save()
    if ns.worker_timeout:
        from .util import write_json
        o = read_json(r.dir / "options.json", {}); o["worker_timeout_minutes"] = float(ns.worker_timeout)
        write_json(r.dir / "options.json", o)
    co = Coordinator(cfg, repo)
    co.execute(r)
    r.reload()
    print(f"verdict: {r.state.verdict}  report: {r.dir / 'report.md'}")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 3, "CANCELLED": 4}.get(r.state.verdict or "", 3)


def cmd_cancel(ns) -> int:
    from .coordinator import cancel_run
    res = cancel_run(ns.run_id)
    print(json.dumps(res))
    return 0


def cmd_cleanup(ns) -> int:
    """Re-run the real-backend account sweep for a finished run (kind firebase-real) and clear its marker."""
    from .coordinator import config_for_run
    from .firebase_real import run_cleanup, account_prefix
    from .runstore import Run
    r = Run(ns.run_id)
    ctx = read_json(r.dir / "ctx.json", {})
    b = ctx.get("backend") or {}
    if b.get("kind") != "firebase-real":
        eprint(f"run {ns.run_id} did not use a real backend (kind {b.get('kind', 'none')}); nothing to clean")
        return 0
    if r.coordinator_alive() and r.state.coordinator_pid != os.getpid():
        eprint(f"coordinator pid {r.state.coordinator_pid} is still running; it cleans up itself when it finishes")
        return 2
    cfg = config_for_run(r)
    d = b.get("detail") or {}
    vals = {"run_id": r.id, "run_dir": str(r.dir), "checkout_dir": str(r.checkout_dir), "artifacts_dir": str(r.artifacts_dir),
            "evidence_dir": str(r.evidence_dir), "cache_dir": str(home_dir() / "cache")}
    res = run_cleanup(cfg.data.get("backend") or {}, checkout=r.checkout_dir, values=vals, run_id=r.id, project_id=d.get("project_id", ""),
                      storage_bucket=d.get("storage_bucket"), log_file=r.artifacts_dir / "backend-cleanup.log", record_to=r.commands_log, log=eprint)
    ctx["backend_cleanup"] = res
    from .util import write_json
    write_json(r.dir / "ctx.json", ctx)
    print(json.dumps({k: res.get(k) for k in ("ok", "project_id", "account_prefix", "deleted_users", "deleted_trees", "remaining", "verified", "error")}))
    if not res["ok"]:
        eprint(f"still needs cleanup: {res['error']}. Accounts start with {account_prefix(r.id)}; the sweep log is {res['log_file']}")
    return 0 if res["ok"] else 1


def cmd_publish_issues(ns) -> int:
    """Opt-in: file each finding of a finished run as a GitHub issue (idempotent; --dry-run prints them)."""
    from .issues import publish_issues
    try:
        res = publish_issues(ns.run_id, repo_slug=ns.repo_slug, labels=ns.label or [], min_severity=ns.min_severity, dry_run=ns.dry_run, log=eprint)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        eprint(str(e)); return 2
    if ns.json:
        print(json.dumps(res, indent=2))
    else:
        for it in res["issues"]:
            print(f"{it['action']:<18} {it['title']}" + (f"  {it.get('url')}" if it.get("url") else ""))
            if ns.dry_run and it.get("body"):
                print("\n".join("    " + l for l in it["body"].splitlines()))
        print(f"{len(res['issues'])} finding(s) processed for {res['repo']}" + (" (dry run)" if ns.dry_run else ""))
    return 1 if any(i["action"] == "failed" for i in res["issues"]) else 0


def cmd_gate_check(ns) -> int:
    from .gate import check_approval
    cfg, repo = _cfg(ns)
    sha = ns.sha or run(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=15).stdout.strip()
    base_sha = None
    if ns.base:
        base_sha = run(["git", "-C", str(repo), "rev-parse", ns.base], timeout=15).stdout.strip() or None
    res = check_approval(repo, sha, cfg, base_sha)
    if ns.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        if res.get("approved"):
            rec = res.get("record", {})
            print(f"APPROVED {sha[:12]} by run {rec.get('run_id')} at {rec.get('created_at')}" + (f"  warning: {res['warning']}" if res.get("warning") else ""))
        else:
            print(f"NOT APPROVED {sha[:12]}: {res.get('reason')}")
    return 0 if res.get("approved") else 1


def cmd_locks(ns) -> int:
    from .scheduler import list_locks, locks_dir
    for l in list_locks():
        print(f"{l.resource:<40} run={l.owner_run} pid={l.owner_pid} stale={l.stale()} since={l.created_at}")
    if ns.clear_stale:
        n = 0
        for l in list_locks():
            if l.stale():
                (locks_dir() / (l.resource.replace(':', '_').replace('/', '_') + ".lock")).unlink(missing_ok=True); n += 1
        for p in locks_dir().glob("*.lock"):
            d = read_json(p, {})
            from .util import pid_alive
            if d and not pid_alive(d.get("owner_pid")):
                p.unlink(missing_ok=True); n += 1
        print(f"cleared {n} stale lock(s)")
    return 0


def cmd_gc(ns) -> int:
    """Remove old runs (worktrees, artifacts) beyond the retention window; never touches active runs."""
    from .runstore import list_runs, runs_dir
    from . import candidate as cand_mod
    cutoff = time.time() - ns.older_than_days * 86400
    removed = 0
    for st in list_runs(1000):
        d = runs_dir() / st.run_id
        if d.stat().st_mtime > cutoff or st.status == "running":
            continue
        for wt, meta in (st.owned.get("worktree") or {}).items():
            try:
                cand_mod.remove_worktree(Path(meta.get("repo", "")), Path(wt))
            except Exception:  # noqa: BLE001
                shutil.rmtree(wt, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
        print(f"removed run {st.run_id}")
    print(f"gc: removed {removed} run(s) older than {ns.older_than_days} days")
    return 0


def cmd_device(ns) -> int:
    from .device.cli import main as device_main
    return device_main(ns.rest)


def cmd_config(ns) -> int:
    cfg, repo = _cfg(ns)
    print(f"# sources: {[str(s) for s in cfg.sources]}")
    print(json.dumps(cfg.data, indent=2, default=str))
    return 0


# ----------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codeandconfirm", description="One agent builds, the other tries to break it.")
    p.add_argument("--version", action="version", version=f"codeandconfirm {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--repo", help="target repository root (default: current git repo)")
        sp.add_argument("--config", help="explicit codeandconfirm.toml path")
        sp.add_argument("--profile", help="config profile to apply, e.g. pr (default when defined) or full")
        sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("doctor", help="check auth, model, tools, devices, ports, config"); common(sp); sp.set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("init", help="write example configs; --devices creates the dedicated simulator/AVD"); common(sp)
    sp.add_argument("--force", action="store_true"); sp.add_argument("--template", help="use this TOML as the project config")
    sp.add_argument("--devices", action="store_true"); sp.add_argument("--boot", action="store_true", help="also boot the Android emulator")
    sp.add_argument("--headless", action="store_true"); sp.set_defaults(fn=cmd_init)
    sp = sub.add_parser("review", help="run QA on a branch or PR"); common(sp)
    sp.add_argument("--branch"); sp.add_argument("--base"); sp.add_argument("--pr", help="PR URL, owner/repo#N, or N")
    sp.add_argument("--criteria", help="acceptance criteria text"); sp.add_argument("--criteria-file")
    sp.add_argument("--platforms", help="comma list, e.g. ios,android"); sp.add_argument("--no-parallel", action="store_true")
    sp.add_argument("--skip-suites", action="store_true", help="debugging only; the gate will report suites as not executed")
    sp.add_argument("--publish", action="store_true", help="post a PR comment + commit status (PR candidates only)")
    sp.add_argument("--merge-candidate", action="store_true", help="test the PR merge commit instead of the head")
    sp.add_argument("--headless-android", action="store_true"); sp.add_argument("--reset-repairs", action="store_true")
    sp.add_argument("--worker-timeout", type=float, help="minutes per Codex worker")
    sp.add_argument("--prepare-only", action="store_true", help="create the run and exit (resume later)")
    sp.add_argument("--perf-only", action="store_true", help="benchmark only: build base + candidate, measure, no suites/QA, no approval")
    sp.set_defaults(fn=cmd_review)
    sp = sub.add_parser("bench", help="controlled base-vs-candidate benchmark (same as review --perf-only)"); common(sp)
    sp.add_argument("--branch"); sp.add_argument("--base"); sp.add_argument("--pr"); sp.add_argument("--platforms")
    sp.add_argument("--headless-android", action="store_true"); sp.add_argument("--prepare-only", action="store_true")
    sp.set_defaults(fn=cmd_review, perf_only=True, no_parallel=False, skip_suites=False, publish=False, merge_candidate=False,
                    reset_repairs=False, criteria=None, criteria_file=None, worker_timeout=None)
    sp = sub.add_parser("status", help="show a run, or list recent runs"); sp.add_argument("run_id", nargs="?"); sp.add_argument("--limit", type=int, default=20); sp.add_argument("--json", action="store_true"); sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("report", help="print a run's report"); sp.add_argument("run_id"); sp.add_argument("--json", action="store_true"); sp.set_defaults(fn=cmd_report)
    sp = sub.add_parser("resume", help="continue an interrupted run"); sp.add_argument("run_id"); sp.add_argument("--force", action="store_true", help="re-evaluate the gate of a finished run")
    sp.add_argument("--rerun-phase", action="append", help="re-run this phase (e.g. suites, qa) before re-gating; repeatable. Re-running qa repeats only workers without a PASS/FAIL verdict")
    sp.add_argument("--worker-timeout", type=float, help="minutes per Codex worker for the resumed phases (recorded in the run's options)"); sp.set_defaults(fn=cmd_resume)
    sp = sub.add_parser("cancel", help="cancel a run and clean up owned resources"); sp.add_argument("run_id"); sp.set_defaults(fn=cmd_cancel)
    sp = sub.add_parser("cleanup", help="re-run the real-backend account sweep for a run flagged NEEDS CLEANUP"); sp.add_argument("run_id"); sp.set_defaults(fn=cmd_cleanup)
    sp = sub.add_parser("publish-issues", help="opt-in: file a run's findings as GitHub issues (idempotent)"); sp.add_argument("run_id")
    sp.add_argument("--repo-slug", help="owner/repo (default: the PR's repo, else the target repo's origin)")
    sp.add_argument("--min-severity", default="low", help="info|low|medium|high|critical (default low)")
    sp.add_argument("--label", action="append", help="label to apply; repeatable (must exist in the repo)")
    sp.add_argument("--dry-run", action="store_true", help="print the issues instead of creating them"); sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_publish_issues)
    sp = sub.add_parser("gate-check", help="is there a current PASS for a commit? (used by hooks)"); common(sp)
    sp.add_argument("--sha"); sp.add_argument("--base"); sp.set_defaults(fn=cmd_gate_check)
    sp = sub.add_parser("locks", help="list reservations"); sp.add_argument("--clear-stale", action="store_true"); sp.set_defaults(fn=cmd_locks)
    sp = sub.add_parser("gc", help="delete old runs and their worktrees"); sp.add_argument("--older-than-days", type=int, default=14); sp.set_defaults(fn=cmd_gc)
    sp = sub.add_parser("device", help="run the device adapter (same as `ccdevice`)"); sp.add_argument("rest", nargs=argparse.REMAINDER); sp.set_defaults(fn=cmd_device)
    sp = sub.add_parser("config", help="print the effective configuration"); common(sp); sp.set_defaults(fn=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        return int(ns.fn(ns) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
