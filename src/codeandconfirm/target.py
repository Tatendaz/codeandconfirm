"""Deterministic build / install / suite execution driven by the project config.

Everything here is run by the coordinator (not the QA model), with real exit codes and
result artifacts recorded to the run, so the gate can verify outcomes independently.
"""

from __future__ import annotations

import os
import shlex
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import Config, render, render_env
from .device.android_adb import AndroidEmulator, apk_identity
from .device.base import Evidence
from .device.ios_idb import IOSSimulator, app_identity
from .results import SuiteResult, parse_results
from .util import CmdResult, expand, run


@dataclass
class BuildOutcome:
    platform: str
    ok: bool
    exit_code: int | None
    duration_s: float
    log_file: str
    artifact: str | None
    identity: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SuiteOutcome:
    name: str
    platform: str
    command: str
    exit_code: int | None
    timed_out: bool
    duration_s: float
    log_file: str
    result: SuiteResult
    started_at: str
    attempts: int = 1
    flaky_history: list[dict] = field(default_factory=list)

    @property
    def green(self) -> bool:
        # A suite without a results artifact (kind "none") is judged by its exit code alone.
        return self.exit_code == 0 and not self.timed_out and (self.result.green or self.result.kind == "none")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["result"] = self.result.to_dict()
        d["green"] = self.green
        return d


def tool_env(cfg: Config) -> dict[str, str]:
    """Resolve tool homes once: config → env → common Homebrew locations."""
    tools = dict(cfg.data.get("tools", {}))
    out = {}
    out["jdk17_home"] = tools.get("jdk17_home") or _first_existing(["/opt/homebrew/opt/openjdk@17", "/usr/local/opt/openjdk@17"]) or ""
    out["jdk21_home"] = tools.get("jdk21_home") or _first_existing(["/opt/homebrew/opt/openjdk@21", "/opt/homebrew/opt/openjdk", "/usr/local/opt/openjdk"]) or ""
    out["android_sdk"] = str(expand(tools.get("android_sdk") or os.environ.get("ANDROID_HOME") or "~/Library/Android/sdk"))
    return out


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if Path(p).exists():
            return p
    return None


def build_platform(platform: str, pcfg: dict, *, checkout: Path, values: dict, log_file: Path, timeout_s: float,
                   record_to: Path) -> BuildOutcome:
    cmd = render(pcfg["build"], values)
    env = render_env(pcfg.get("env"), values)
    t0 = time.monotonic()
    r = run(cmd, cwd=checkout, env=env, timeout=timeout_s, log_file=log_file, record_to=record_to, label=f"build-{platform}")
    dur = round(time.monotonic() - t0, 1)
    if not r.ok:
        return BuildOutcome(platform, False, r.exit_code, dur, str(log_file), None, error=_tail(r))
    art_key = "app" if platform == "ios" else "apk"
    art = Path(render(pcfg[art_key], values))
    if not art.is_absolute():
        art = checkout / art
    if not art.exists():
        return BuildOutcome(platform, False, r.exit_code, dur, str(log_file), str(art), error="build succeeded but artifact missing")
    ident = app_identity(art) if platform == "ios" else apk_identity(art, Path(values["android_sdk"]))
    return BuildOutcome(platform, True, r.exit_code, dur, str(log_file), str(art), ident)


def install_platform(platform: str, pcfg: dict, artifact: Path, device_id: str, evidence_dir: Path, sdk: str | None) -> dict:
    ev = Evidence(evidence_dir, platform, device_id)
    if platform == "ios":
        dev = IOSSimulator(device_id, ev)
        app = pcfg["bundle_id"]
        dev.uninstall(app)
        ident = dev.install(artifact)
        state = dev.app_state(app)
    else:
        dev = AndroidEmulator(device_id, ev, sdk=sdk)
        pkg = pcfg["package"]
        dev.set_test_friendly()          # every run, not only after a boot: a long-lived emulator keeps stale state
        dev.uninstall(pkg)
        ident = dev.install(artifact)
        state = dev.app_state(pkg)
    installed = (state.get("identity") or {}).get("sha256")
    built = ident.get("sha256")
    matched = bool(installed and built and (installed == built or platform == "ios"))
    # iOS: simctl copies the bundle; the executable hash inside the container is identical to the built one.
    if platform == "ios":
        matched = bool(installed and built and installed == built)
    rec = {"platform": platform, "device": device_id, "built": ident, "installed": state.get("identity"),
           "identity_match": matched, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (evidence_dir / "build-identity.json").write_text(__import__("json").dumps(rec, indent=2, default=str))
    return rec


def run_suite(name: str, scfg: dict, *, checkout: Path, values: dict, artifacts_dir: Path, timeout_s: float,
              record_to: Path, retries: int = 0) -> SuiteOutcome:
    cmd = render(scfg["command"], values)
    env = render_env(scfg.get("env"), values)
    rk = (scfg.get("results") or {}).get("kind", "none")
    rpath = render((scfg.get("results") or {}).get("path", ""), values, strict=False)
    rp = Path(rpath) if rpath else Path("/nonexistent")
    if rpath and not rp.is_absolute():
        rp = checkout / rp
    history: list[dict] = []
    attempts = 0
    while True:
        attempts += 1
        if rk == "xcresult" and rp.exists():
            run(["rm", "-rf", str(rp)])
        log_file = artifacts_dir / f"suite-{name}{'' if attempts == 1 else f'-attempt{attempts}'}.log"
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        t0 = time.monotonic()
        r = run(cmd, cwd=checkout, env=env, timeout=timeout_s, log_file=log_file, record_to=record_to, label=f"suite-{name}")
        dur = round(time.monotonic() - t0, 1)
        res = parse_results(rk, rp)
        out = SuiteOutcome(name, scfg.get("platform", "n/a"), cmd, r.exit_code, r.timed_out, dur, str(log_file), res, started,
                           attempts, history)
        infra_failure = (not out.green) and (not res.parsed or res.total == 0) and attempts <= retries
        if out.green or not infra_failure:
            return out
        history.append({"attempt": attempts, "exit_code": r.exit_code, "timed_out": r.timed_out, "detail": res.detail,
                        "classification": "infrastructure (no test results produced)"})


def _tail(r: CmdResult, n: int = 1500) -> str:
    return (r.stderr or r.stdout)[-n:]
