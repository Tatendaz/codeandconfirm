"""The coordinator: runs a review end to end, durably, with explicit resume/cancel/timeout.

Phases (each recorded in run.json; `resume` continues from the first phase that is not ok):
  reserving → checkout → backend → build → install → suites → qa → perf → gate

Deterministic work (checkout, builds, installs, adapter proofs, native suites, backend) is done
here with real exit codes. The Codex workers only get the prepared environment plus the
device adapter; their verdicts are validated by the gate against the evidence on disk.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import candidate as cand_mod
from .backend import BackendError, start_backend, stop_backend
from . import firebase_real
from .claude_worker import ClaudeWorker, resolve_claude
from .codex_worker import CodexWorker, WorkerSpec, load_role, resolve_codex, PROMPTS_DIR
from .config import Config, home_dir, render
from .device.android_adb import AndroidEmulator, boot_emulator, ensure_avd
from .device.base import DeviceError
from .device.ios_idb import IOSSimulator, ensure_simulator
from .gate import approve, evaluate, qa_engine, qa_model_requested, repo_key
from .report import short_summary, toolchain_facts, write_report
from .runstore import Run, new_run_id, list_runs
from .scheduler import Reservation, acquire_functional_slot, release_all_for, wait_for_capacity, host_metrics
from .target import BuildOutcome, build_platform, install_platform, run_suite, tool_env
from .util import (CommandFailed, append_jsonl, eprint, kill_process_group, now_iso, pid_alive, read_json, run,
                   write_json)


class Cancelled(Exception):
    pass


class InfraBlocked(Exception):
    pass


@dataclass
class ReviewOptions:
    platforms: list[str] | None = None
    parallel: bool | None = None
    skip_suites: bool = False
    publish: bool = False
    merge_candidate: bool = False
    headless_android: bool = False
    reset_repairs: bool = False
    criteria: str = ""
    worker_timeout_minutes: float | None = None
    dry_run: bool = False
    perf_only: bool = False


class Coordinator:
    def __init__(self, cfg: Config, repo_root: Path, log=None):
        self.cfg = cfg
        self.repo_root = Path(repo_root)
        self.log = log or (lambda s: eprint(f"[codeandconfirm] {s}"))
        self.run: Run | None = None
        self.ctx: dict = {}
        self._reservations: list[Reservation] = []
        self._workers: dict[str, CodexWorker] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ starting
    def start_branch(self, branch: str, base: str | None, opts: ReviewOptions) -> Run:
        base = base or self.cfg.get("project.default_base", "main")
        c = cand_mod.resolve_branch(self.repo_root, branch, base, opts.criteria)
        return self._create_run(c, opts)

    def start_pr(self, spec: str, opts: ReviewOptions) -> Run:
        from .github import repo_slug_from_remote
        slug = repo_slug_from_remote(self.repo_root)
        c = cand_mod.resolve_pr(spec, self.repo_root, slug, opts.criteria, merge_candidate=opts.merge_candidate)
        return self._create_run(c, opts)

    def _create_run(self, c: cand_mod.Candidate, opts: ReviewOptions) -> Run:
        run_ = Run(new_run_id(), create=True, repo_root=self.repo_root)
        run_.state.config_sources = [str(s) for s in self.cfg.sources]
        run_.state.model_requested = qa_model_requested(self.cfg)
        run_.state.profile = self.cfg.get("profile.name") or None
        run_.state.qa_engine = qa_engine(self.cfg)
        run_.save_candidate(c.to_dict())
        opts_d = {k: v for k, v in opts.__dict__.items()}
        write_json(run_.dir / "options.json", opts_d)
        # Snapshot the effective configuration: a resumed run must use the plan it started with.
        write_json(run_.dir / "config.snapshot.json", {"data": self.cfg.data, "sources": [str(s) for s in self.cfg.sources]})
        # durable repair counter, keyed by repo + candidate ref, survives sessions
        cyc = self._repair_cycle_for(c, reset=opts.reset_repairs)
        run_.state.repair_cycle = cyc
        run_.save()
        self.run = run_
        self.log(f"run {run_.id}: candidate {c.candidate_sha[:10]} ({c.candidate_ref}) vs base {c.base_sha[:10]} ({c.base_ref}); repair cycle {cyc}"
                 + (f"; profile {run_.state.profile}" if run_.state.profile else ""))
        return run_

    def _repairs_path(self) -> Path:
        d = home_dir() / "repairs"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{repo_key(self.repo_root)}.json"

    def _repair_cycle_for(self, c: cand_mod.Candidate, reset: bool = False) -> int:
        data = read_json(self._repairs_path(), {})
        key = c.candidate_ref
        ent = data.get(key, {"cycle": 0, "last_verdict": None, "last_sha": None})
        if reset:
            ent = {"cycle": 0, "last_verdict": None, "last_sha": None}
        elif ent.get("last_verdict") == "FAIL" and ent.get("last_sha") != c.candidate_sha:
            ent["cycle"] = int(ent.get("cycle", 0)) + 1
        data[key] = ent
        write_json(self._repairs_path(), data)
        return int(ent["cycle"])

    def _record_repair_outcome(self, c: dict, verdict: str) -> None:
        data = read_json(self._repairs_path(), {})
        ent = data.get(c["candidate_ref"], {"cycle": 0})
        ent.update(last_verdict=verdict, last_sha=c["candidate_sha"], last_run=self.run.id if self.run else None, updated_at=now_iso())
        if verdict == "PASS":
            ent["cycle"] = 0
        data[c["candidate_ref"]] = ent
        write_json(self._repairs_path(), data)

    # ------------------------------------------------------------------ execution
    def execute(self, run_: Run) -> Run:
        self.run = run_
        run_.state.coordinator_pid = os.getpid()
        run_.save()
        self.ctx = read_json(run_.dir / "ctx.json", {}) or {}
        self.ctx.setdefault("run_started_at", run_.state.created_at)
        self.ctx.setdefault("platform_status", {})
        self._install_signal_handlers()
        cand = run_.candidate()
        opts = ReviewOptions(**read_json(run_.dir / "options.json", {}))
        self.ctx["platforms"] = self._platforms(opts)   # what THIS run selected; the gate judges only these
        max_cycles = int(self.cfg.get("qa.max_repair_cycles", 5))
        try:
            if run_.state.repair_cycle > max_cycles:
                raise InfraBlocked(f"repair budget exhausted: cycle {run_.state.repair_cycle} > max {max_cycles}; a human should review this branch")
            if cand.get("trust") == "untrusted" and self.cfg.get("trust.external_prs", "static-only") == "refuse":
                raise InfraBlocked("candidate comes from a fork and [trust].external_prs = refuse")
            self._phase("reserving", self._phase_reserving, cand, opts)
            self._phase("checkout", self._phase_checkout, cand, opts)
            self._phase("backend", self._phase_backend, cand, opts)
            self._phase("build", self._phase_build, cand, opts)
            self._phase("install", self._phase_install, cand, opts)
            self._phase("suites", self._phase_suites, cand, opts)
            self._phase("qa", self._phase_qa, cand, opts)
            self._phase("perf", self._phase_perf, cand, opts)
            self._phase("gate", self._phase_gate, cand, opts)
        except Cancelled:
            self.ctx["cancelled"] = True
            self._finalize(cand, opts, cancelled=True)
        except InfraBlocked as e:
            self.ctx.setdefault("infra_blocks", []).append(str(e))
            self.log(f"BLOCKED: {e}")
            self._finalize(cand, opts, infra_block=str(e))
        except Exception as e:  # noqa: BLE001 — never lose the run state on an unexpected error
            self.ctx.setdefault("infra_blocks", []).append(f"unexpected error: {type(e).__name__}: {e}")
            self.log(f"unexpected error: {type(e).__name__}: {e}")
            self._finalize(cand, opts, infra_block=f"{type(e).__name__}: {e}")
            raise
        finally:
            self._save_ctx()
            self._cleanup()
        return run_

    def _phase(self, name: str, fn, cand: dict, opts: ReviewOptions) -> None:
        if self.run.phase_status(name) == "ok":
            self.log(f"phase {name}: already done (resume)")
            return
        self._check_cancel()
        self.run.phase_start(name)
        t0 = time.monotonic()
        try:
            fn(cand, opts)
        except (Cancelled, InfraBlocked):
            self.run.phase_end(name, "blocked")
            raise
        except Exception as e:
            self.run.phase_end(name, "failed", error=f"{type(e).__name__}: {e}")
            raise
        self.run.phase_end(name, "ok", seconds=round(time.monotonic() - t0, 1))
        self._save_ctx()

    def _save_ctx(self) -> None:
        if self.run:
            write_json(self.run.dir / "ctx.json", self.ctx)

    def _check_cancel(self) -> None:
        if self.run and self.run.cancel_requested():
            raise Cancelled()

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            self.log(f"signal {signum}: cancelling run")
            if self.run:
                self.run.request_cancel()
            for w in list(self._workers.values()):
                w.cancel()
            from .util import kill_all_live
            kill_all_live()
        for s in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(s, handler)
            except ValueError:
                pass  # not main thread

    # ------------------------------------------------------------------ values
    def _values(self) -> dict:
        r = self.run
        v = {"run_id": r.id, "run_dir": str(r.dir), "checkout_dir": str(r.checkout_dir), "artifacts_dir": str(r.artifacts_dir),
             "evidence_dir": str(r.evidence_dir), "cache_dir": str(home_dir() / "cache"), **tool_env(self.cfg)}
        cand = r.candidate()
        v["candidate_sha"] = cand.get("candidate_sha"); v["base_sha"] = cand.get("base_sha")
        devs = self.ctx.get("devices", {})
        v["ios_udid"] = (devs.get("ios") or {}).get("id")
        v["android_serial"] = (devs.get("android") or {}).get("id")
        for k, p in ((self.ctx.get("backend") or {}).get("ports") or {}).items():
            v[f"{k}_port"] = p
        for k in ("auth", "firestore", "storage", "functions"):
            v.setdefault(f"{k}_port", (self.cfg.get("backend.ports") or {}).get(k, ""))
        bd = (self.ctx.get("backend") or {}).get("detail") or {}
        v["project_id"] = bd.get("project_id", ""); v["storage_bucket"] = bd.get("storage_bucket") or ""
        v["account_prefix"] = firebase_real.account_prefix(r.id)
        (home_dir() / "cache").mkdir(parents=True, exist_ok=True)
        return v

    def _platforms(self, opts: ReviewOptions) -> list[str]:
        want = opts.platforms or self.cfg.get("qa.platforms", [])
        return [p for p in want if p in self.cfg.data.get("platforms", {})]

    def _active_platforms(self, opts: ReviewOptions) -> list[str]:
        return [p for p in self._platforms(opts) if not (self.ctx["platform_status"].get(p) or {}).get("blocked")]

    # ------------------------------------------------------------------ phases
    def _phase_reserving(self, cand: dict, opts: ReviewOptions) -> None:
        # A (re-)reservation re-evaluates every platform: a block recorded by an earlier attempt (device
        # unavailable, adapter proof failed) is not carried over, so `resume --rerun-phase reserving …` retries it.
        for p in self._platforms(opts):
            (self.ctx.get("platform_status") or {}).pop(p, None)
        sched = self.cfg.data["scheduler"]
        m = host_metrics(sched.get("ci_worker_process_pattern", ""))
        self.ctx["host_at_start"] = m.to_dict()
        self.log(f"host: {m.cpu_count} cores, load {m.load_1m}, {m.mem_free_percent}% memory free, {m.ci_workers_busy} CI job(s) busy")
        d = wait_for_capacity(sched, should_abort=lambda: bool(self.run and self.run.cancel_requested()), log=self.log)
        self._check_cancel()
        if not d.ok:
            raise InfraBlocked(f"host capacity not available: {d.reason}")
        slot = acquire_functional_slot(self.run.id, sched, wait_s=float(sched.get("wait_for_capacity_minutes", 20)) * 60)
        if slot is None:
            raise InfraBlocked("no functional-run slot became free (other CodeAndConfirm runs hold them)")
        self._reservations.append(slot)
        self.run.own("reservation", slot.resource)
        ttl = float(sched.get("lock_ttl_minutes", 180)) * 60
        devices: dict[str, dict] = self.ctx.get("devices", {})
        for p in self._platforms(opts):
            pcfg = self.cfg.data["platforms"][p]
            try:
                devices[p] = self._reserve_device(p, pcfg, opts, ttl)
                self.log(f"{p}: device {devices[p]['id']} ({devices[p]['name']}, {devices[p]['os']})")
            except (DeviceError, CommandFailed) as e:
                self.ctx["platform_status"][p] = {"blocked": f"device unavailable: {e}"}
                self.log(f"{p}: BLOCKED — {e}")
        self.ctx["devices"] = devices
        self.ctx["platforms"] = self._platforms(opts)
        self.ctx["toolchain"] = toolchain_facts()

    def _reserve_device(self, p: str, pcfg: dict, opts: ReviewOptions, ttl: float) -> dict:
        """Pick the first free device from the configured pool (device_names / avd_names+ports), creating or
        booting it on demand; reserve it for this run. Raises DeviceError when every candidate is taken."""
        errors = []
        if p == "ios":
            names = pcfg.get("device_names") or [pcfg.get("device_name", "CodeAndConfirm iPhone")]
            for name in names:
                info = ensure_simulator(name, pcfg.get("device_type", "iPhone 17"))
                ident = info["udid"]
                res = Reservation(f"ios-device:{ident}", self.run.id, ttl, "dedicated QA device")
                if not res.try_acquire():
                    cur = res.current(); errors.append(f"{name} reserved by run {cur.owner_run if cur else '?'}"); continue
                dev = IOSSimulator(ident)
                ok, why = dev.available()
                if not ok:
                    res.release(); errors.append(f"{name}: not booted ({why})"); continue
                if info["created"]:
                    self.run.own("simulator", ident, created=True)
                self._reservations.append(res)
                return {"id": ident, "name": name, "os": why}
            raise DeviceError("no free iOS simulator in the pool: " + "; ".join(errors))
        sdk = tool_env(self.cfg)["android_sdk"]
        avds = pcfg.get("avd_names") or [pcfg.get("avd_name", "codeandconfirm_api34")]
        ports = pcfg.get("ports") or [int(pcfg.get("port", 5580))]
        for avd, port in zip(avds, ports):
            serial = f"emulator-{port}"
            res = Reservation(f"android-device:{serial}", self.run.id, ttl, "dedicated QA device")
            if not res.try_acquire():
                cur = res.current(); errors.append(f"{serial} reserved by run {cur.owner_run if cur else '?'}"); continue
            try:
                ensure_avd(avd, sdk=sdk)
                info = boot_emulator(avd, port, headless=opts.headless_android, log_file=self.run.artifacts_dir / f"emulator-{port}.log", sdk=sdk)
            except DeviceError as e:
                res.release(); errors.append(f"{avd}: {e}"); continue
            if not info.get("already_running") and info.get("pid"):
                self.run.own("emulator", serial, pid=info["pid"])
                self.run.record_pid(f"emulator-{port}", info["pid"])
            dev = AndroidEmulator(serial, sdk=sdk)
            rel = dev._shell("getprop", "ro.build.version.release", check=False).strip()
            self._reservations.append(res)
            return {"id": serial, "name": avd, "os": f"Android {rel}"}
        raise DeviceError("no free Android emulator in the pool: " + "; ".join(errors))

    def _phase_checkout(self, cand: dict, opts: ReviewOptions) -> None:
        src = Path(cand["repo_root"])
        dest = self.run.checkout_dir
        cand_mod.create_worktree(src, cand["candidate_sha"], dest)
        self.run.own("worktree", str(dest), repo=str(src))
        self.ctx["checkout_head_start"] = cand_mod.head_sha(dest)
        cand_mod.write_diff(src, cand["merge_base_sha"], cand["candidate_sha"], self.run.dir / "candidate.diff")
        self.log(f"checkout at {self.ctx['checkout_head_start'][:10]} → {dest}")

    def _phase_backend(self, cand: dict, opts: ReviewOptions) -> None:
        bcfg = self.cfg.data.get("backend", {"kind": "none"})
        if bcfg.get("kind", "none") == "none" or not self._active_platforms(opts):
            self.ctx["backend"] = {"kind": "none", "ports": {}}
            return
        ttl = float(self.cfg.get("scheduler.lock_ttl_minutes", 180)) * 60
        try:
            h = start_backend(bcfg, checkout=self.run.checkout_dir, values=self._values(), run_id=self.run.id,
                              log_file=self.run.artifacts_dir / "backend.log", ttl_s=ttl,
                              wait_s=float(self.cfg.get("scheduler.wait_for_capacity_minutes", 20)) * 60, log=self.log,
                              base_ports_required=("android" in self._active_platforms(opts)))
        except BackendError as e:
            raise InfraBlocked(f"backend: {e}") from e
        self.ctx["backend"] = h.to_dict()
        if bcfg.get("kind") == "firebase-emulator":
            self.ctx.setdefault("coordinator_modified", []).append(f"{bcfg.get('workdir', 'firebase')}/firebase.json")
        if bcfg.get("kind") == "firebase-real":
            # the swap script rewrites these tracked files (and marks them skip-worktree); that is ours, not the worker's
            self.ctx.setdefault("coordinator_modified", []).extend(bcfg.get("config_files") or [])
            self.ctx["backend_cleanup"] = None      # pending until _finalize; a crash before then leaves it None → flagged
        if h.pid:
            self.run.own("backend", str(h.pid), backend_kind=h.kind)
            self.run.record_pid("backend", h.pid)
        self.log(f"backend {h.kind} up on ports {h.ports} (pid {h.pid})")

    def _phase_build(self, cand: dict, opts: ReviewOptions) -> None:
        vals = self._values()
        builds: dict[str, dict] = self.ctx.get("builds", {})
        todo = [p for p in self._active_platforms(opts) if not builds.get(p, {}).get("ok")]
        if not todo:
            return

        def do(p: str) -> tuple[str, BuildOutcome]:
            self.log(f"{p}: building…")
            out = build_platform(p, self.cfg.data["platforms"][p], checkout=self.run.checkout_dir, values=vals,
                                 log_file=self.run.artifacts_dir / f"build-{p}.log", timeout_s=45 * 60, record_to=self.run.commands_log)
            self.log(f"{p}: build {'ok' if out.ok else 'FAILED'} in {out.duration_s}s")
            return p, out
        with ThreadPoolExecutor(max_workers=len(todo)) as ex:
            for p, out in ex.map(do, todo):
                builds[p] = self._snapshot_artifact(p, out)
        self.ctx["builds"] = builds
        self._check_cancel()
        if self.cfg.get("qa.base_comparison", True) and not opts.perf_only:
            self._build_base(cand, opts)

    def _build_base(self, cand: dict, opts: ReviewOptions) -> None:
        """Build the base SHA too (own worktree, same commands) so device workers can compare suspicious
        behaviour against it: `ccdevice <platform> install --base` / `--candidate`."""
        base_dir = self.run.base_checkout_dir
        try:
            cand_mod.create_worktree(Path(cand["repo_root"]), cand["base_sha"], base_dir)
        except CommandFailed as e:
            self.ctx.setdefault("notes", []).append(f"base worktree unavailable: {e}")
            return
        self.run.own("worktree", str(base_dir), repo=cand["repo_root"])
        bcfg = self.cfg.data.get("backend") or {}
        if bcfg.get("kind") == "firebase-real" and (self.ctx.get("backend") or {}).get("kind") == "firebase-real":
            try:
                firebase_real.swap_env(bcfg, base_dir, log_file=self.run.artifacts_dir / "backend.log", record_to=self.run.commands_log)
            except firebase_real.RealBackendRefused as e:
                self.ctx.setdefault("notes", []).append(f"base build not pointed at the real project ({e}); base comparison unavailable")
                return
        vals = dict(self._values(), run_dir=str(self.run.dir / "base"), checkout_dir=str(base_dir))
        (self.run.dir / "base").mkdir(exist_ok=True)
        base_builds: dict[str, dict] = self.ctx.get("base_builds", {})
        todo = [p for p in self._active_platforms(opts) if (self.ctx.get("builds", {}).get(p) or {}).get("ok") and not base_builds.get(p, {}).get("ok")]

        def do(p: str) -> tuple[str, BuildOutcome]:
            self.log(f"{p}: building base {cand['base_sha'][:10]} for comparison…")
            out = build_platform(p, self.cfg.data["platforms"][p], checkout=base_dir, values=vals,
                                 log_file=self.run.artifacts_dir / f"build-{p}-base.log", timeout_s=45 * 60, record_to=self.run.commands_log)
            self.log(f"{p}: base build {'ok' if out.ok else 'FAILED'} in {out.duration_s}s")
            return p, out
        if todo:
            with ThreadPoolExecutor(max_workers=len(todo)) as ex:
                for p, out in ex.map(do, todo):
                    base_builds[p] = self._snapshot_artifact(p, out, subdir="products-base")
        self.ctx["base_builds"] = base_builds

    def _snapshot_artifact(self, p: str, out: BuildOutcome, subdir: str = "products") -> dict:
        """Copy the built artifact into the run's own products directory. Native test runners
        (`xcodebuild test`, `gradlew connected…Test`) rebuild the app in place, so the file at the build
        path can change after the build phase; installs, identity checks and benchmarks must use the copy."""
        d = out.to_dict()
        if not out.ok or not out.artifact:
            return d
        from .device.android_adb import apk_identity
        from .device.ios_idb import app_identity
        src = Path(out.artifact)
        dest_dir = self.run.artifacts_dir / subdir / p
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        run(["rm", "-rf", str(dest)])
        run(["cp", "-R", str(src), str(dest)], check=True)
        d["built_at_path"] = str(src)
        d["artifact"] = str(dest)
        d["identity"] = app_identity(dest) if p == "ios" else apk_identity(dest, Path(self._values()["android_sdk"]))
        return d

    def _phase_install(self, cand: dict, opts: ReviewOptions) -> None:
        vals = self._values()
        installs = self.ctx.get("installs", {})
        proofs = self.ctx.get("proofs", {})
        bindir = self._bin_dir()
        for p in self._active_platforms(opts):
            b = self.ctx.get("builds", {}).get(p)
            if not b or not b.get("ok"):
                continue
            pcfg = self.cfg.data["platforms"][p]
            dev_id = self.ctx["devices"][p]["id"]
            evd = self.run.evidence_dir / p
            try:
                installs[p] = install_platform(p, pcfg, Path(b["artifact"]), dev_id, evd, vals["android_sdk"])
                self.log(f"{p}: installed; identity match={installs[p]['identity_match']}")
                env = self._worker_env(p, evd)
                r = run([str(bindir / "ccdevice"), p, "proof", "--json"], env=env, timeout=10 * 60,
                        log_file=self.run.artifacts_dir / f"proof-{p}.log", record_to=self.run.commands_log, label=f"proof-{p}")
                proofs[p] = read_json(evd / "proof.json", {"ok": False, "error": "proof produced no record", "steps": []})
                self.log(f"{p}: adapter proof {'ok' if proofs[p].get('ok') else 'FAILED: ' + str(proofs[p].get('error'))}")
                if not proofs[p].get("ok"):
                    self.ctx["platform_status"][p] = {"blocked": f"device adapter proof failed: {proofs[p].get('error')}"}
            except (DeviceError, CommandFailed) as e:
                self.ctx["platform_status"][p] = {"blocked": f"install failed: {e}"}
                self.log(f"{p}: BLOCKED — install failed: {e}")
        self.ctx["installs"] = installs
        self.ctx["proofs"] = proofs

    def _phase_suites(self, cand: dict, opts: ReviewOptions) -> None:
        if opts.perf_only:
            self.ctx.setdefault("notes", []).append("benchmark-only run: suites skipped")
            return
        if opts.skip_suites:
            self.ctx.setdefault("notes", []).append("suites skipped by --skip-suites (gate will report them as not executed)")
            return
        vals = self._values()
        suites = self.ctx.get("suites", {})
        names = [n for n in self.cfg.get("qa.required_suites", []) if n in self.cfg.suites]
        groups: dict[str, list[str]] = {}
        for n in names:
            if n in suites and suites[n].get("green"):
                continue
            plat = self.cfg.suites[n].get("platform", "backend")
            if plat in ("ios", "android") and plat not in self._active_platforms(opts):
                continue
            if plat in ("ios", "android") and not (self.ctx.get("builds", {}).get(plat) or {}).get("ok"):
                continue
            groups.setdefault(plat, []).append(n)
        if not groups:
            return
        retries = int(self.cfg.get("qa.infra_retries", 1))

        def do_group(plat: str) -> list[tuple[str, dict]]:
            out = []
            for n in groups[plat]:
                self._check_cancel()
                self.log(f"suite {n} ({plat}): running…")
                o = run_suite(n, self.cfg.suites[n], checkout=self.run.checkout_dir, values=vals, artifacts_dir=self.run.artifacts_dir,
                              timeout_s=45 * 60, record_to=self.run.commands_log, retries=retries)
                self.log(f"suite {n}: exit={o.exit_code} {o.result.passed}/{o.result.total} passed, failed={o.result.failed} ({o.duration_s}s)")
                out.append((n, o.to_dict()))
            return out
        with ThreadPoolExecutor(max_workers=len(groups)) as ex:
            for res in ex.map(do_group, list(groups)):
                for n, d in res:
                    suites[n] = d
        self.ctx["suites"] = suites
        self._post_suite_device_check(opts)

    def _post_suite_device_check(self, opts: ReviewOptions) -> None:
        """Native suites change device state: Gradle's connected tests uninstall the app afterwards and
        XCUITest sessions can wedge the simulator's accessibility bridge. Re-install the exact candidate
        artifact, re-verify its identity, and heal the simulator before the QA workers get the devices."""
        vals = self._values()
        for p in self._active_platforms(opts):
            b = (self.ctx.get("builds") or {}).get(p)
            if not b or not b.get("ok"):
                continue
            pcfg = self.cfg.data["platforms"][p]
            dev_id = self.ctx["devices"][p]["id"]
            evd = self.run.evidence_dir / p
            try:
                rec = install_platform(p, pcfg, Path(b["artifact"]), dev_id, evd, vals["android_sdk"])
                self.ctx.setdefault("installs", {})[p] = rec
                self.log(f"{p}: re-installed candidate after suites; identity match={rec['identity_match']}")
                if p == "ios":
                    dev = IOSSimulator(dev_id)
                    dev.launch(pcfg["bundle_id"], list(pcfg.get("launch_args", [])) + list(pcfg.get("reset_args", [])))
                    time.sleep(4)
                    if not dev.accessibility_healthy():
                        self.log("ios: accessibility bridge unhealthy after suites; recovering (backboardd restart, then reboot if needed)")
                        r = dev.recover_accessibility(pcfg["bundle_id"], list(pcfg.get("launch_args", [])) + list(pcfg.get("reset_args", [])))
                        self.ctx.setdefault("notes", []).append(f"ios simulator rebooted to recover accessibility after suites: {r}")
                        if not r["ok"]:
                            self.ctx["platform_status"]["ios"] = {"blocked": "simulator accessibility bridge could not be recovered after the native suites"}
            except (DeviceError, CommandFailed) as e:
                self.ctx["platform_status"][p] = {"blocked": f"device check after suites failed: {e}"}
                self.log(f"{p}: BLOCKED — {e}")

    def _phase_qa(self, cand: dict, opts: ReviewOptions) -> None:
        if opts.perf_only:
            self.ctx.setdefault("notes", []).append("benchmark-only run: QA workers skipped")
            return
        engine = qa_engine(self.cfg)
        if engine == "claude":
            rc = resolve_claude(self.cfg.get("claude.binary", ""), self.cfg.get("claude.model"))
        elif engine == "codex":
            rc = resolve_codex(self.cfg.get("codex.binary", ""), self.cfg.get("codex.model"))
        else:
            raise InfraBlocked(f"[roles].qa = {engine!r} is not supported (codex | claude)")
        if not rc["ok"]:
            raise InfraBlocked(f"{engine}: {rc['reason']}")
        self.ctx["codex"] = {"binary": rc["binary"], "version": rc["version"], "model": rc["model"], "model_info": rc.get("model_info"), "engine": engine}
        self.ctx["roles"] = {"lead": self.cfg.get("roles.lead", "claude"), "qa": engine}
        workers_out: dict[str, dict] = self.ctx.get("workers", {})
        names: list[str] = []
        if self.cfg.get("qa.static_review", True):
            names.append("review")
        names += [p for p in self._active_platforms(opts) if (self.ctx.get("builds", {}).get(p) or {}).get("ok")]
        names = [n for n in names if worker_needs_run(workers_out.get(n))]
        if not names:
            return
        parallel = self.cfg.get("qa.parallel_platforms", True) if opts.parallel is None else opts.parallel
        wt = float(opts.worker_timeout_minutes or self.cfg.get("qa.worker_timeout_minutes", 60)) * 60
        specs = {n: self._worker_spec(n, cand, rc, wt) for n in names}
        self._save_ctx()

        def run_worker(n: str) -> tuple[str, dict]:
            attempts = 0
            while True:
                attempts += 1
                w = ClaudeWorker(specs[n]) if engine == "claude" else CodexWorker(specs[n])
                with self._lock:
                    self._workers[n] = w
                pid = w.start()
                self.run.record_pid(f"worker-{n}", pid)
                self.log(f"worker {n}: started {engine} pid {pid} (model {specs[n].model}, effort {specs[n].reasoning_effort})")
                res = w.wait(should_cancel=lambda: bool(self.run.cancel_requested()), heartbeat=self._heartbeat)
                d = res.to_dict(); d["attempt"] = attempts
                infra = (not res.cancelled) and (not res.timed_out) and (res.verdict is None) \
                    and attempts <= int(self.cfg.get("qa.infra_retries", 1)) and not self.run.cancel_requested()
                if infra:
                    self.log(f"worker {n}: no verdict (exit {res.exit_code}); retrying once as an infrastructure failure")
                    d["retried"] = True
                    self.ctx.setdefault("worker_attempts", []).append(d)
                    continue
                self.log(f"worker {n}: finished exit={res.exit_code} verdict={(res.verdict or {}).get('verdict')} model_used={res.model_used}")
                return n, d
        if parallel:
            with ThreadPoolExecutor(max_workers=len(names)) as ex:
                for n, d in ex.map(run_worker, names):
                    workers_out[n] = d
        else:
            for n in names:
                n2, d = run_worker(n)
                workers_out[n2] = d
                self._save_ctx()
        self.ctx["workers"] = workers_out
        if self.run.cancel_requested():
            raise Cancelled()
        self._final_identity_check(opts)

    def _final_identity_check(self, opts: ReviewOptions) -> None:
        """After QA, the device must still carry the candidate build (workers may have swapped to the base)."""
        vals = self._values()
        out: dict[str, dict] = {}
        for p in self._active_platforms(opts):
            b = (self.ctx.get("builds") or {}).get(p)
            if not b or not b.get("ok"):
                continue
            pcfg = self.cfg.data["platforms"][p]
            dev_id = self.ctx["devices"][p]["id"]
            try:
                dev = IOSSimulator(dev_id) if p == "ios" else AndroidEmulator(dev_id, sdk=vals["android_sdk"])
                app = pcfg["bundle_id"] if p == "ios" else pcfg["package"]
                st = dev.app_state(app)
                installed = (st.get("identity") or {}).get("sha256")
                want = (b.get("identity") or {}).get("sha256")
                out[p] = {"match": bool(installed and want and installed == want), "installed": installed, "candidate": want}
            except DeviceError as e:
                out[p] = {"match": False, "error": str(e)}
        self.ctx["final_identity"] = out

    def _phase_perf(self, cand: dict, opts: ReviewOptions) -> None:
        if not (self.cfg.get("perf.enabled", False) or opts.perf_only):
            self.ctx["perf"] = None
            return
        from .perf import run_perf
        self.ctx["perf"] = run_perf(self, cand, opts)

    def _phase_gate(self, cand: dict, opts: ReviewOptions) -> None:
        self._finalize(cand, opts)

    # ------------------------------------------------------------------ finalize
    def _finalize(self, cand: dict, opts: ReviewOptions, *, cancelled: bool = False, infra_block: str | None = None) -> None:
        if self.run.state.verdict:
            return
        self._real_backend_cleanup()
        co = self.run.checkout_dir
        if co.exists():
            try:
                self.ctx["checkout_head_end"] = cand_mod.head_sha(co)
                allowed = tuple(self.ctx.get("coordinator_modified", []))
                self.ctx["pristine"] = list(cand_mod.worktree_is_pristine(co, allowed_prefixes=allowed))
            except CommandFailed as e:
                self.ctx["checkout_head_end"] = None
                self.ctx["pristine"] = [False, [f"git status failed: {e}"]]
        else:
            self.ctx["checkout_head_end"] = None
            self.ctx["pristine"] = [False, ["checkout missing"]]
        self.ctx["cancelled"] = cancelled
        self.ctx["evidence_root"] = str(self.run.evidence_dir)
        self.ctx["perf_only"] = bool(opts.perf_only)
        gate = evaluate(self.cfg, cand, self.ctx)
        if infra_block:
            # An infrastructure failure means we did not finish testing: BLOCKED, unless a real product
            # failure (failed suite, blocking finding, failed build) was already established.
            product_fail = any((not ch.ok) and ch.outcome == "fail" and (ch.category in ("suite", "finding") or ch.name.startswith("build."))
                               for ch in gate.checks)
            if not product_fail:
                gate.verdict = "BLOCKED"
            gate.block_reasons.insert(0, infra_block)
        self.run.state.model_used = ",".join(sorted({m for w in (self.ctx.get("workers") or {}).values() for m in (w.get("model_used") or [])})) or None
        md, js = write_report(self.run.dir, self.run.state.__dict__, cand, self.cfg.data, self.ctx, gate)
        if gate.verdict in ("PASS", "FAIL") and not opts.perf_only:
            # PASS records the approval; FAIL overwrites an older PASS for the same SHA (a later failure wins).
            # BLOCKED/CANCELLED add no evidence either way and leave existing records alone.
            ap = approve(self.run.id, str(self.repo_root), cand, self.cfg, gate, {"report": str(md), "profile": self.run.state.profile})
            self.ctx["approval"] = str(ap)
        self._record_repair_outcome(cand, gate.verdict)
        self.run.finish(gate.verdict, "; ".join((gate.fail_reasons or gate.block_reasons)[:3]))
        self.ctx["gate"] = gate.to_dict()
        self.log(short_summary(gate, cand, self.run.id))
        self.log(f"report: {md}")
        # A cancelled run is not a verdict: nothing is published for it (a later run posts the real one).
        if (opts.publish or self.cfg.get("github.publish")) and cand.get("pr") and gate.verdict != "CANCELLED":
            from .github import publish_comment, publish_status
            pr = cand["pr"]
            c = publish_comment(pr["repo"], pr["number"], md)
            s = publish_status(pr["repo"], cand["candidate_sha"], gate.verdict, self.cfg.get("github.status_context", "codeandconfirm/qa"),
                               f"CodeAndConfirm {gate.verdict} (run {self.run.id})")
            self.ctx["published"] = {"comment": c, "status": s}
            self.log(f"published: comment ok={c.get('ok')} status ok={s.get('ok')}")

    def _real_backend_cleanup(self) -> None:
        """Delete every account this run created in the real project (kind firebase-real). Runs once, before the
        gate, so the report carries the result; failure is flagged loudly but never changes the product verdict."""
        b = self.ctx.get("backend") or {}
        if b.get("kind") != "firebase-real" or self.ctx.get("backend_cleanup"):
            return
        bcfg = self.cfg.data.get("backend") or {}
        d = b.get("detail") or {}
        self.ctx["backend_cleanup"] = firebase_real.run_cleanup(
            bcfg, checkout=self.run.checkout_dir, values=self._values(), run_id=self.run.id, project_id=d.get("project_id", ""),
            storage_bucket=d.get("storage_bucket"), log_file=self.run.artifacts_dir / "backend-cleanup.log",
            record_to=self.run.commands_log, log=self.log)
        self._save_ctx()

    def _cleanup(self) -> None:
        if not self.run:
            return
        stop_backend(self.ctx.get("backend"), self.run.id)
        if (self.ctx.get("backend") or {}).get("kind") == "firebase-real":
            bcfg = self.cfg.data.get("backend") or {}
            for co in (self.run.checkout_dir, self.run.base_checkout_dir):
                firebase_real.scrub_real_configs(bcfg, co, log_file=self.run.artifacts_dir / "backend.log")
        for r in self._reservations:
            r.release()
        release_all_for(self.run.id)
        self._save_ctx()

    def _heartbeat(self) -> None:
        with self._lock:
            for r in self._reservations:
                try:
                    r.renew()
                except OSError as e:  # never let bookkeeping kill a worker wait loop
                    self.log(f"heartbeat: could not renew {r.resource}: {e}")

    # ------------------------------------------------------------------ workers
    def _bin_dir(self) -> Path:
        b = self.run.dir / "bin"
        b.mkdir(exist_ok=True)
        wrapper = b / "ccdevice"
        wrapper.write_text(f"#!/bin/bash\nexec {json.dumps(sys.executable)} -m codeandconfirm.device.cli \"$@\"\n")
        wrapper.chmod(0o755)
        return b

    def _worker_env(self, platform: str | None, evidence_dir: Path) -> dict[str, str]:
        vals = self._values()
        env = {"CAC_RUN_ID": self.run.id, "CAC_EVIDENCE_DIR": str(evidence_dir), "CAC_ANDROID_SDK": vals["android_sdk"],
               "PATH": f"{self._bin_dir()}:{os.environ.get('PATH', '')}", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        if platform == "ios":
            p = self.cfg.data["platforms"]["ios"]
            env.update(CAC_IOS_UDID=vals["ios_udid"] or "", CAC_IOS_BUNDLE=p["bundle_id"], CAC_IOS_LAUNCH_ARGS=" ".join(p.get("launch_args", [])),
                       CAC_IOS_RESET_ARGS=" ".join(p.get("reset_args", [])),
                       CAC_IOS_APP_PATH=(self.ctx.get("builds", {}).get("ios") or {}).get("artifact", ""),
                       CAC_IOS_BASE_APP_PATH=(self.ctx.get("base_builds", {}).get("ios") or {}).get("artifact", "") or "")
        if platform == "android":
            p = self.cfg.data["platforms"]["android"]
            env.update(CAC_ANDROID_SERIAL=vals["android_serial"] or "", CAC_ANDROID_PACKAGE=p["package"], CAC_ANDROID_ACTIVITY=p.get("activity", ".MainActivity"),
                       CAC_ANDROID_LAUNCH_ARGS=" ".join(p.get("launch_args", [])), CAC_ANDROID_RESET_ARGS=" ".join(p.get("reset_args", [])),
                       CAC_ANDROID_APK=(self.ctx.get("builds", {}).get("android") or {}).get("artifact", ""),
                       CAC_ANDROID_BASE_APK=(self.ctx.get("base_builds", {}).get("android") or {}).get("artifact", "") or "")
        return env

    def _worker_spec(self, name: str, cand: dict, rc: dict, timeout_s: float) -> WorkerSpec:
        ws = self.run.codex_dir / name
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "AGENTS.md").write_text(load_role("qa_role.md"))
        link = ws / "checkout"
        if not link.exists():
            link.symlink_to(self.run.checkout_dir)
        shutil.copy(self.run.dir / "candidate.diff", ws / "candidate.diff")
        (ws / "patches").mkdir(exist_ok=True)
        platform = name if name in ("ios", "android") else None
        evd = self.run.evidence_dir / name
        evd.mkdir(parents=True, exist_ok=True)
        prompt = self._task_text(name, platform, cand, evd, timeout_s)
        (ws / "task.md").write_text(prompt)
        trust = cand.get("trust", "trusted")
        sandbox = self.cfg.get("codex.sandbox", "danger-full-access") if trust == "trusted" else "read-only"
        engine = qa_engine(self.cfg)
        effort = (self.cfg.get("claude.reasoning_effort") or self.cfg.get("codex.reasoning_effort", "high")) if engine == "claude" \
            else self.cfg.get("codex.reasoning_effort", "high")
        extra = self.cfg.get("claude.extra_args" if engine == "claude" else "codex.extra_args", []) or []
        return WorkerSpec(name=name, workspace=ws, prompt=prompt, binary=rc["binary"], model=rc["model"],
                          reasoning_effort=effort, sandbox=sandbox, timeout_s=timeout_s,
                          env=self._worker_env(platform, evd), extra_args=list(extra))

    def _task_text(self, name: str, platform: str | None, cand: dict, evd: Path, timeout_s: float) -> str:
        tpl = (PROMPTS_DIR / "task_template.md").read_text()
        vals = self._values()
        devs = self.ctx.get("devices", {})
        backend = self.ctx.get("backend") or {}
        env_lines = []
        if backend.get("kind") == "firebase-emulator":
            env_lines.append(f"- The coordinator rewrote `{self.cfg.get('backend.workdir', 'firebase')}/firebase.json` to pin the emulator ports for this run; "
                             f"that tracked-file change is expected and whitelisted — it is not a modification of the candidate and not a blocker.")
        if backend.get("kind") == "firebase-real":
            bd = backend.get("detail") or {}
            env_lines.append(f"- Backend: **REAL Firebase project `{bd.get('project_id')}`** (environment `{bd.get('env_name', 'dev')}`; a shared cloud "
                             f"development project, NOT an emulator). Say so in your summary. Every account you create MUST start with "
                             f"`{bd.get('account_prefix')}`: the coordinator deletes exactly those accounts and their data afterwards. Never create, edit or "
                             f"delete anything else there, never sign in to an account you did not create in this run, and do not use admin credentials or "
                             f"the Firebase console. Backend inspection is limited to what the signed-in test account can read.")
        elif backend.get("kind", "none") != "none":
            env_lines.append(f"- Backend: **{backend['kind']}** on 127.0.0.1 ports {backend.get('ports')} (Android reaches the host as 10.0.2.2). "
                             f"Log: `{backend.get('log_file')}`. This is a LOCAL emulator, not the real cloud backend; say so in your summary.")
        if platform:
            d = devs.get(platform, {})
            b = self.ctx.get("builds", {}).get(platform, {})
            ident = b.get("identity", {})
            inst = self.ctx.get("installs", {}).get(platform, {})
            env_lines.append(f"- Device (yours alone): **{platform} `{d.get('id')}`** ({d.get('name')}, {d.get('os')}). Do not use any other device.")
            env_lines.append(f"- Installed build: `{Path(b.get('artifact', '')).name}` version {ident.get('version')} sha256 `{ident.get('sha256')}` "
                             f"(identity match at install: {inst.get('identity_match')}). Verify with `ccdevice {platform} app-state` before and after.")
            if platform == "ios":
                env_lines.append(f"- App: bundle `{self.cfg.get('platforms.ios.bundle_id')}`. `ccdevice ios launch --reset` clears data and launches with reset args "
                                 f"{self.cfg.get('platforms.ios.reset_args', [])} (fresh signed-out state); `ccdevice ios launch` / `restart` keep the session (args {self.cfg.get('platforms.ios.launch_args', [])}).")
            else:
                env_lines.append(f"- App: package `{self.cfg.get('platforms.android.package')}` activity `{self.cfg.get('platforms.android.activity', '.MainActivity')}`.")
            env_lines.append(f"- Adapter proof already passed on this device: see `{evd / 'proof.json'}` and its screenshots.")
            env_lines.append("- `ccdevice … tap <text> --times 2 --interval-ms 80` delivers a real rapid double-tap from one process; use it for duplicate-submission checks. "
                             "Switches/checkboxes are tapped at their trailing edge automatically (`--anchor center` overrides). System permission alerts appear in the tree: tap their button explicitly.")
            if platform == "ios":
                env_lines.append("- Navigation-bar/toolbar buttons (the + button, Save/Edit/Back in the bar) are NOT in `ccdevice ios tree` on this iOS version. "
                                 "`ccdevice ios bar` lists them (hit-tested), and `ccdevice ios tap '<bar label>'` finds them automatically when the label is not in the tree. "
                                 "Last resort: screenshot pixels ÷ 3 = points (`ccdevice ios scale`), then `--xy X Y`; `ccdevice ios describe-point X Y` tells you what is there.")
                env_lines.append("- `ccdevice ios launch --reset` wipes the app's data container and the simulator keychain (no reinstall). If `ccdevice ios tree` ever returns only the "
                                 "Application node while the screenshot shows UI, run `ccdevice ios recover-accessibility` once (~10 s; restarts the simulator's UI server and relaunches the app) and continue; record it in `blockers` only if it fails.")
            env_lines.append(f"- Test accounts: use `{firebase_real.account_prefix(self.run.id)}{platform}-<n>@example.test` with password `CacTest-1234`. Never use real accounts.")
            bb = (self.ctx.get("base_builds") or {}).get(platform) or {}
            if bb.get("ok"):
                env_lines.append(f"- Base build available for comparison: `{Path(bb.get('artifact', '')).name}` (base {cand.get('base_sha', '')[:10]}, sha256 `{(bb.get('identity') or {}).get('sha256')}`). "
                                 f"To attribute a suspicious behaviour: `ccdevice {platform} install --base`, reproduce the exact steps, record the result in `compared_to_base`, "
                                 f"then ALWAYS `ccdevice {platform} install --candidate` and re-check `app-state` before continuing. A device left on the base build invalidates the run.")
            else:
                env_lines.append("- No base build is available in this run; write `compared_to_base: \"not compared\"`.")
            env_lines.append(f"- Verdict policy: `block_severity = {self.cfg.get('qa.block_severity', 'high')}`. Return `FAIL` only for findings at/above that severity or a failed required journey; "
                             f"report lower-severity defects in `findings` with verdict `PASS`. Return `BLOCKED` when coverage was not obtained.")
        else:
            env_lines.append("- No device is assigned to this worker. Do not run `ccdevice` against any device; read code, run device-free suites only.")
        env_lines.append(f"- Command log the coordinator keeps: `{self.run.commands_log}`")
        suites_lines = []
        for sname, s in (self.ctx.get("suites") or {}).items():
            r = s.get("result", {})
            suites_lines.append(f"- **{sname}** ({s.get('platform')}): exit {s.get('exit_code')}, {r.get('passed')}/{r.get('total')} passed, "
                                f"failed {r.get('failed')} · log `{s.get('log_file')}`" + (f" · result `{r.get('path')}`" if r.get("path") else ""))
            suites_lines.append(f"  command: `{s.get('command')}`")
        if not suites_lines:
            suites_lines.append("- (none executed yet)")
        catalog = self.cfg.get("journeys.catalog", {}) or {}
        req = list(self.cfg.get("qa.required_journeys", []) or self.cfg.get("journeys.required", []))
        journeys_lines = [f"- `{j}` — {catalog.get(j, '(no description)')}" for j in req] if platform else ["- (review worker: report one scenario per review area instead: review.correctness, review.security, review.data-integrity, review.deployment, review.parity, review.performance)"]
        extra = [f"- `{j}` — {d}" for j, d in catalog.items() if j not in req]
        if platform and extra:
            journeys_lines.append("\nOptional journeys (cover when time allows):")
            journeys_lines += extra
        prof = self.cfg.get("profile.name") or ""
        scope_line = ""
        if self.cfg.get("qa.require_diff_scenarios"):
            scope_line = (f"\n8. Scope ({prof or 'diff-focused'} profile): the required journeys are the minimum; spend the rest of your budget on the behaviour "
                          f"this diff changes and report it as `diff.<slug>` scenarios (at least one when the diff touches your platform; the gate checks). "
                          f"Skip optional journeys unless the diff touches them.")
        if platform:
            title = f"hands-on functional, visual and accessibility QA on {platform}"
            body = (
                f"1. Confirm the foreground app is the assigned build (`ccdevice {platform} app-state`).\n"
                f"2. Drive every required journey through the real UI with `ccdevice {platform} …`, reading the tree after each action. "
                f"Screenshot each meaningful state.\n"
                f"3. Derive scenarios from the diff (`candidate.diff`) and exercise the changed behaviour end to end; try the unexpected "
                f"(double taps, rapid retries, empty/whitespace input, back mid-flow, keyboard covering controls, restart, offline if the app supports it).\n"
                f"4. Inspect empty/loading/error states, clipped or overlapping text, safe areas, touch targets and accessibility labels; check `ccdevice {platform} logs` for crashes/exceptions after each journey.\n"
                f"5. Inspect backend state when it matters (the local emulator's REST API / logs) to confirm exactly one record was created where the UI shows one.\n"
                f"6. If you find a defect, write concrete reproduction steps, keep the evidence, and where practical propose a regression test as a patch file.\n"
                f"7. Base comparison is not provisioned for this worker unless a base build is listed above; write `compared_to_base: \"not compared\"` otherwise."
                + scope_line
            )
        else:
            title = "independent code review of the diff (correctness, security, data integrity, deployment, parity, performance)"
            body = (
                "1. Read `candidate.diff` and the touched files in `checkout/`. Trace behaviour end to end (UI → view model → repository → backend rules/functions).\n"
                "2. Check: correctness and edge cases; security (auth, rules, injection, secrets, data exposure); data integrity (duplicates, idempotency, partial writes, "
                "migrations); deployment dependencies (backend rules/functions/indexes/config that must ship together — name them explicitly); iOS/Android parity; "
                "performance risks (N+1 reads, heavy work on the main thread, unbounded lists, large images).\n"
                "3. Run the device-free suites that exist in the checkout (unit tests, rules tests) when they are quick; record exit codes; save relevant output as .txt in your evidence dir.\n"
                "4. Do not restate style nits. Prioritise defects that would ship. Each finding: file:line, why it is wrong, how to reproduce or a test that would catch it.\n"
                "5. Report one scenario per review area with status passed (no issue found) or failed (finding raised)."
            )
        changed = cand.get("changed_files") or []
        cf_block = "\n".join(f"  - `{f}`" for f in changed[:80]) + (f"\n  - … {len(changed) - 80} more" if len(changed) > 80 else "")
        source_desc = (f"PR {cand['pr'].get('repo')}#{cand['pr'].get('number')} — {cand['pr'].get('title')}" if cand.get("pr")
                       else f"local branch `{cand.get('candidate_ref')}` in {cand.get('repo_root')}")
        text = tpl.format(
            run_id=self.run.id, worker=name, project_name=self.cfg.get("project.name") or self.repo_root.name,
            candidate_sha=cand.get("candidate_sha"), candidate_ref=cand.get("candidate_ref"), tested_ref_kind=cand.get("tested_ref_kind", "head"),
            base_sha=cand.get("base_sha"), base_ref=cand.get("base_ref"), merge_base_sha=cand.get("merge_base_sha"), source_desc=source_desc,
            trust=cand.get("trust", "trusted"), checkout_dir=self.run.checkout_dir, changed_count=len(changed), changed_files_block=cf_block or "  - (none)",
            acceptance_block=cand.get("acceptance_criteria") or "(none provided — derive expectations from the diff, commit messages and existing tests)",
            assignment_title=title, assignment_body=body, environment_block="\n".join(env_lines), suites_block="\n".join(suites_lines),
            required_journey_count=len(req) if platform else 6, journeys_block="\n".join(journeys_lines), evidence_dir=evd,
            platform_hint=platform or "ios", budget_minutes=int(timeout_s / 60) - 5,
        )
        return text


# ---------------------------------------------------------------------- cancel / resume helpers


def worker_needs_run(entry: dict | None) -> bool:
    """A worker is re-run on resume unless it already produced a final verdict (PASS or FAIL). BLOCKED means
    coverage was not obtained — a resume (`--rerun-phase qa`) tries again; so does a missing or malformed verdict."""
    v = ((entry or {}).get("verdict") or {}).get("verdict")
    return v not in ("PASS", "FAIL")


def config_for_run(run_: Run) -> Config:
    """The configuration snapshot taken when the run was created (falls back to a fresh load)."""
    snap = read_json(run_.dir / "config.snapshot.json", None)
    repo = Path(run_.state.repo_root)
    if snap and isinstance(snap.get("data"), dict):
        return Config(snap["data"], [Path(s) for s in snap.get("sources", [])], repo)
    return Config.load(repo_root=repo)



def cancel_run(run_id: str, log=eprint) -> dict:
    r = Run(run_id)
    r.request_cancel()
    killed = []
    if r.coordinator_alive() and r.state.coordinator_pid != os.getpid():
        try:
            os.kill(r.state.coordinator_pid, signal.SIGTERM)
            killed.append(f"coordinator:{r.state.coordinator_pid}")
        except ProcessLookupError:
            pass
        # give it a moment to finalize itself
        for _ in range(60):
            time.sleep(0.5)
            r.reload()
            if r.state.verdict:
                break
    for name, pid in r.pids().items():
        if name == "emulator":
            continue  # devices are reusable; leave them unless owned+created (handled below)
        if pid_alive(pid):
            kill_process_group(pid)
            killed.append(f"{name}:{pid}")
    ctx = read_json(r.dir / "ctx.json", {})
    stop_backend(ctx.get("backend"), run_id)
    if (ctx.get("backend") or {}).get("kind") == "firebase-real" and not (ctx.get("backend_cleanup") or {}).get("ok"):
        cfg = config_for_run(r)
        d = (ctx.get("backend") or {}).get("detail") or {}
        vals = {"run_id": run_id, "run_dir": str(r.dir), "checkout_dir": str(r.checkout_dir), "artifacts_dir": str(r.artifacts_dir)}
        ctx["backend_cleanup"] = firebase_real.run_cleanup(cfg.data.get("backend") or {}, checkout=r.checkout_dir, values=vals, run_id=run_id,
                                                           project_id=d.get("project_id", ""), storage_bucket=d.get("storage_bucket"),
                                                           log_file=r.artifacts_dir / "backend-cleanup.log", log=log)
        write_json(r.dir / "ctx.json", ctx)
    released = release_all_for(run_id)
    owned = r.state.owned
    for serial, meta in (owned.get("emulator") or {}).items():
        pid = meta.get("pid")
        if pid and pid_alive(pid):
            kill_process_group(pid)
            killed.append(f"emulator:{serial}")
    r.reload()
    if not r.state.verdict:
        r.finish("CANCELLED", "cancelled by user")
        ctx["cancelled"] = True
        write_json(r.dir / "ctx.json", ctx)
    log(f"run {run_id} cancelled; killed {killed or 'nothing'}; released locks {released or 'none'}")
    return {"run_id": run_id, "killed": killed, "released": released, "verdict": r.state.verdict}
