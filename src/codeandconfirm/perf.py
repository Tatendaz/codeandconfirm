"""Controlled performance comparison (candidate vs base) — preliminary on simulators/emulators.

Implemented measurements:
  startup   cold-start time. Android: `am start -W` TotalTime (ms). iOS: time from `simctl launch`
            until the app's first accessibility element is visible (ms, polled every 50 ms).
  memory    resident memory shortly after launch (kB; Android PSS via dumpsys, iOS RSS of the
            simulated process).

Method: the base SHA is built into its own worktree with the same toolchain and installed on the
SAME device; each journey is sampled N times per build, interleaved (base, candidate, base, …) to
spread drift evenly. The comparison uses medians; the noise floor is the larger of the configured
budget and 3× the base median absolute deviation. Anything else is reported as inconclusive,
never as "no regression".
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from . import candidate as cand_mod
from .device.android_adb import AndroidEmulator
from .device.ios_idb import IOSSimulator
from .scheduler import capacity_check
from .target import build_platform, tool_env
from .util import read_json, run, write_json


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else float("nan")


def _mad(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = statistics.median(xs)
    return statistics.median([abs(x - m) for x in xs])


def _ios_startup_ms(dev: IOSSimulator, bundle: str, args: list[str], timeout_s: float = 30,
                    ready_marker: str | None = None) -> tuple[float | None, int | None]:
    """Cold start → ready, plus resident memory ~1.5 s after ready. Returns (startup_ms, memory_kb).

    With `ready_marker` (a line the app prints to stdout once it is ready) the app is launched with
    `simctl launch --console-pty`, and the time from the launch call to the marker is measured on the host
    (stdout via a PTY is line-buffered, so the marker arrives as soon as the app prints it; observed jitter on
    the reference host was ±20 ms). Without a marker, the accessibility snapshot is polled every 100 ms until
    interactive elements appear (coarser, but identical for base and candidate)."""
    import os
    import select
    import subprocess
    dev.terminate(bundle)
    time.sleep(1.0)
    if ready_marker:
        t0 = time.monotonic()
        proc = subprocess.Popen(["xcrun", "simctl", "launch", "--console-pty", "--terminate-running-process", dev.udid, bundle, *args],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        hit = None
        buf = b""
        fd = proc.stdout.fileno()
        while time.monotonic() - t0 < timeout_s:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                buf += chunk
                if ready_marker.encode() in buf:
                    hit = (time.monotonic() - t0) * 1000
                    break
        mem = None
        if hit is not None:
            time.sleep(1.5)
            mem = dev.memory_kb(bundle)
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        return hit, mem
    t_wall = time.monotonic()
    dev.launch(bundle, args)
    while time.monotonic() - t_wall < timeout_s:
        r = run(["idb", "ui", "describe-all", "--udid", dev.udid], timeout=30)
        try:
            els = json.loads(r.stdout) if r.ok else []
        except json.JSONDecodeError:
            els = []
        if len(els) > 3 and any(e.get("type") in ("Button", "TextField", "StaticText") for e in els[1:]):
            ms = (time.monotonic() - t_wall) * 1000
            time.sleep(1.5)
            return ms, dev.memory_kb(bundle)
        time.sleep(0.1)
    return None, None


def _android_startup_ms(dev: AndroidEmulator, comp: str) -> float | None:
    """Cold start via `am start -W -S`; TotalTime is reported by the system in ms (first frame)."""
    dev.terminate(comp)
    time.sleep(1.0)
    info = dev.launch(comp)
    return float(info["startup_total_ms"]) if info.get("startup_total_ms") is not None else None


def compare_series(base: list[float], cand: list[float], pct_budget: float, min_samples: int = 3) -> dict:
    """Median comparison with a noise floor of max(3×MAD(base), base_median×budget%)."""
    if len(base) < min_samples or len(cand) < min_samples:
        return {"verdict": "inconclusive", "reason": f"insufficient samples (base {len(base)}, candidate {len(cand)})"}
    mb, mc = _median(base), _median(cand)
    noise = max(3 * _mad(base), mb * pct_budget / 100.0)
    delta = mc - mb
    verdict = "regression" if delta > noise else ("improvement" if delta < -noise else "within noise")
    return {"base_median": round(mb, 1), "candidate_median": round(mc, 1), "delta": round(delta, 1),
            "threshold": round(noise, 1), "verdict": verdict, "base_mad": round(_mad(base), 1), "n": min(len(base), len(cand))}


def run_perf(coord, cand: dict, opts) -> dict:
    cfg = coord.cfg
    out: dict = {"journeys": {}, "regressions": [], "inconclusive": [], "table": [], "summary": "", "device_class": "simulator/emulator (preliminary)"}
    samples = int(cfg.get("perf.samples", 5))
    budgets = cfg.get("perf.budgets", {}) or {}
    pct_budget = float(budgets.get("startup_ms_percent", 25))
    # Builds and installs just ran, so the 1-minute load average lags; give the host time to settle.
    settle_s = float(cfg.get("perf.settle_wait_minutes", 5)) * 60
    t0 = time.monotonic()
    while True:
        d = capacity_check(cfg.data["scheduler"], need_exclusive=True)
        if d.ok or time.monotonic() - t0 > settle_s:
            break
        coord.log(f"perf: waiting for an idle host ({d.reason})")
        time.sleep(15)
    out["host"] = d.metrics.to_dict()
    if not d.ok:
        out["inconclusive"].append(f"benchmark skipped: host not exclusive after {int(settle_s)}s ({d.reason})")
        out["summary"] = "Performance not measured: the host was busy; benchmarks need exclusive access."
        return out
    # build base into its own worktree
    src = Path(cand["repo_root"])
    base_dir = coord.run.base_checkout_dir
    cand_mod.create_worktree(src, cand["base_sha"], base_dir)
    coord.run.own("worktree", str(base_dir), repo=str(src))
    vals = coord._values()
    base_vals = dict(vals, run_dir=str(coord.run.dir / "base"), checkout_dir=str(base_dir))
    (coord.run.dir / "base").mkdir(exist_ok=True)
    for p in coord._active_platforms(opts):
        coord._check_cancel()
        pcfg = cfg.data["platforms"][p]
        cb = coord.ctx.get("builds", {}).get(p)
        if not cb or not cb.get("ok"):
            out["inconclusive"].append(f"{p}: candidate build missing"); continue
        bb = build_platform(p, pcfg, checkout=base_dir, values=base_vals, log_file=coord.run.artifacts_dir / f"build-{p}-base.log",
                            timeout_s=45 * 60, record_to=coord.run.commands_log)
        if not bb.ok:
            out["inconclusive"].append(f"{p}: base build failed (exit {bb.exit_code})"); continue
        dev_id = coord.ctx["devices"][p]["id"]
        dev = IOSSimulator(dev_id) if p == "ios" else AndroidEmulator(dev_id, sdk=vals["android_sdk"])
        app = pcfg["bundle_id"] if p == "ios" else f"{pcfg['package']}/{pcfg.get('activity', '.MainActivity')}"
        args = pcfg.get("launch_args", []) if p == "ios" else []
        series = {"base": {"startup_ms": [], "memory_kb": []}, "candidate": {"startup_ms": [], "memory_kb": []}}
        builds = {"base": Path(bb.artifact), "candidate": Path(cb["artifact"])}
        for i in range(samples):
            for label in ("base", "candidate"):   # interleaved
                coord.log(f"perf {p}: sample {i + 1}/{samples} {label}")
                dev.uninstall(app.split("/")[0]); dev.install(builds[label])
                if p == "ios":
                    ms, mem = _ios_startup_ms(dev, app, args, ready_marker=cfg.get("perf.ios_ready_log_marker") or None)
                else:
                    ms = _android_startup_ms(dev, app)
                    time.sleep(1.5)
                    mem = dev.memory_kb(app)
                if ms is not None:
                    series[label]["startup_ms"].append(ms)
                if mem:
                    series[label]["memory_kb"].append(mem)
        # restore the candidate build on the device for any later inspection
        dev.uninstall(app.split("/")[0]); dev.install(builds["candidate"])
        res = {"samples": samples, "series": series}
        for metric in ("startup_ms", "memory_kb"):
            b, c = series["base"][metric], series["candidate"][metric]
            cmp = compare_series(b, c, pct_budget)
            res[metric] = cmp
            if cmp["verdict"] == "inconclusive":
                out["inconclusive"].append(f"{p} {metric}: {cmp['reason']}"); continue
            out["table"].append(f"{p} {metric}: base {cmp['base_median']:.0f} → candidate {cmp['candidate_median']:.0f} "
                                f"(Δ {cmp['delta']:+.0f}, threshold ±{cmp['threshold']:.0f}, n={cmp['n']}) → {cmp['verdict']}")
            if cmp["verdict"] == "regression":
                out["regressions"].append(f"{p} {metric} +{cmp['delta']:.0f} (> {cmp['threshold']:.0f})")
        out["journeys"][p] = res
    write_json(coord.run.artifacts_dir / "perf.json", out)
    out["summary"] = ("Measured on the same device with interleaved samples; medians compared against max(budget, 3×MAD) noise floor. "
                      "Simulator/emulator numbers are preliminary signals, not device performance.")
    return out
