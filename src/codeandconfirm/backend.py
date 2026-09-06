"""Backend lifecycle for a run (local emulators / mock servers), with port handling.

Kinds:
  none               nothing to start
  command            run `[backend].start` in `[backend].workdir` and wait for `health` URLs
  firebase-emulator  like `command`, but the checkout's firebase.json is rewritten to the
                     ports chosen for this run (UI disabled, hub/logging pinned) so concurrent
                     suites on one host cannot collide.
  firebase-real      no local process: the checkout's client configs are swapped to the named
                     environment (development project only, allow-listed by project id) and every
                     `cac-<run-id>-` account is deleted again after QA. See firebase_real.py.

Port isolation modes ([backend].port_isolation):
  none      always the configured base ports; a foreign listener → BLOCKED
  per-run   allocate a free port band per run (apps must accept the ports via placeholders)
  ios-only  the Android app hardcodes host ports, so the run uses the base ports and holds a
            host-wide reservation on them; a foreign listener → BLOCKED (never shared state)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import render, render_env
from .firebase_real import (DEFAULT_HEALTH, RealBackendRefused, account_prefix, health_ok as real_health_ok,
                            swap_env)
from .scheduler import Reservation, ports_in_use
from .util import kill_process_group, now_iso, pid_alive, read_json, run, write_json


@dataclass
class BackendHandle:
    kind: str
    pid: int | None
    ports: dict[str, int]
    log_file: str | None
    started_at: str
    reservation: str | None = None
    already_running: bool = False
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class BackendError(RuntimeError):
    pass


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def allocate_ports(base: dict[str, int], mode: str, run_id: str, ttl_s: float, wait_s: float = 0.0,
                   log=lambda s: None, base_ports_required: bool = True) -> tuple[dict[str, int], Reservation | None, str]:
    """Return (ports, reservation, note). Raises BackendError when the required ports are foreign-held.

    mode "ios-only" means: the iOS app accepts per-run ports but the Android app hardcodes the base ports.
    When no Android platform is active in this run (`base_ports_required=False`), a per-run band is used so
    several iOS-only runs can share the host."""
    if not base:
        return {}, None, "no ports declared"
    if mode == "ios-only" and not base_ports_required:
        mode = "per-run"
    if mode in ("none", "ios-only"):
        key = "backend-ports:" + ",".join(str(p) for p in sorted(base.values()))
        res = Reservation(key, run_id, ttl_s, "backend base ports")
        if not res.try_acquire():
            cur = res.current()
            log(f"backend ports {sorted(base.values())} held by run {cur.owner_run if cur else '?'}; waiting up to {int(wait_s)}s")
            if not res.acquire(wait_s):
                cur = res.current()
                raise BackendError(f"base ports {sorted(base.values())} reserved by run {cur.owner_run if cur else '?'}; "
                                   f"the Android app hardcodes these host ports, so this run cannot start in parallel")
        busy = ports_in_use(base.values())
        if busy:
            res.release()
            raise BackendError(f"base ports already in use by another process: {busy}. Stop it or run with "
                               f"per-run port isolation (requires the apps to accept port overrides)")
        return dict(base), res, f"base ports (mode={mode})"
    # per-run: shift the whole band until every port is free
    for shift in range(20000, 60000, 100):
        cand = {k: v + shift for k, v in base.items()}
        if all(_port_free(p) for p in cand.values()):
            key = "backend-ports:" + ",".join(str(p) for p in sorted(cand.values()))
            res = Reservation(key, run_id, ttl_s, "backend per-run ports")
            if res.try_acquire():
                return cand, res, f"per-run ports shifted by +{shift}"
    raise BackendError("could not find a free port band")


def prepare_firebase_json(checkout: Path, workdir: str, ports: dict[str, int]) -> Path:
    fj = checkout / workdir / "firebase.json"
    data = read_json(fj, {})
    em = data.setdefault("emulators", {})
    for name, port in ports.items():
        em.setdefault(name, {})["port"] = port
    em["ui"] = {"enabled": False}
    hub = max(ports.values()) + 4400 if ports else 14400
    em["hub"] = {"port": hub}
    em["logging"] = {"port": hub + 100}
    write_json(fj, data)
    return fj


def _health_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (local health URL)
            return 200 <= resp.status < 500
    except Exception:  # noqa: BLE001
        return False


def start_backend(cfg_backend: dict, *, checkout: Path, values: dict, run_id: str, log_file: Path,
                  ttl_s: float, health_timeout_s: float = 180, wait_s: float = 0.0, log=lambda s: None,
                  base_ports_required: bool = True) -> BackendHandle:
    kind = cfg_backend.get("kind", "none")
    if kind == "none":
        return BackendHandle("none", None, {}, None, now_iso())
    if kind == "firebase-real":
        return start_real_backend(cfg_backend, checkout=checkout, values=values, run_id=run_id, log_file=log_file, log=log)
    ports, reservation, note = allocate_ports({k: int(v) for k, v in (cfg_backend.get("ports") or {}).items()},
                                              cfg_backend.get("port_isolation", "none"), run_id, ttl_s, wait_s, log,
                                              base_ports_required)
    vals = dict(values)
    for k, v in ports.items():
        vals[f"{k}_port"] = v
    workdir = checkout / cfg_backend.get("workdir", ".")
    env = render_env(cfg_backend.get("env"), vals)
    try:
        for prep in cfg_backend.get("prepare", []) or []:
            r = run(render(prep, vals), cwd=checkout, env=env, timeout=900, log_file=log_file, label="backend-prepare")
            if not r.ok:
                raise BackendError(f"backend prepare failed (exit {r.exit_code}): {prep}")
        if kind == "firebase-emulator":
            prepare_firebase_json(checkout, cfg_backend.get("workdir", "firebase"), ports)
        start_cmd = render(cfg_backend["start"], vals)
        lf = open(log_file, "ab")
        lf.write(f"# {now_iso()} $ {start_cmd}\n".encode())
        full_env = dict(os.environ); full_env.update(env)
        proc = subprocess.Popen(["/bin/bash", "-lc", start_cmd], cwd=str(workdir), env=full_env, stdout=lf,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        health = [render(h, vals) for h in cfg_backend.get("health", [])]
        deadline = time.monotonic() + health_timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise BackendError(f"backend exited early (code {proc.returncode}); see {log_file}")
            if all(_health_ok(h) for h in health):
                return BackendHandle(kind, proc.pid, ports, str(log_file), now_iso(),
                                     reservation.resource if reservation else None, detail={"note": note, "health": health})
            time.sleep(1.0)
        kill_process_group(proc.pid)
        raise BackendError(f"backend did not become healthy within {health_timeout_s}s: {health}")
    except Exception:
        if reservation:
            reservation.release()
        raise


def start_real_backend(cfg_backend: dict, *, checkout: Path, values: dict, run_id: str, log_file: Path, log=lambda s: None) -> BackendHandle:
    """Point the checkout at the real development project. Refuses (BackendError → BLOCKED) unless the swapped
    configs name an allow-listed project id; nothing is started or reserved, so several runs may share it —
    they never share accounts, because each run only creates `cac-<run-id>-` users."""
    env = render_env(cfg_backend.get("env"), values)
    for prep in cfg_backend.get("prepare", []) or []:
        r = run(render(prep, values), cwd=checkout, env=env, timeout=900, log_file=log_file, label="backend-prepare")
        if not r.ok:
            raise BackendError(f"backend prepare failed (exit {r.exit_code}): {prep}")
    try:
        info = swap_env(cfg_backend, checkout, log_file=log_file)
    except RealBackendRefused as e:
        raise BackendError(f"real backend refused: {e}") from e
    health = [render(h, values) for h in (cfg_backend.get("health") or DEFAULT_HEALTH)]
    bad = real_health_ok(health)
    if bad:
        raise BackendError(f"real backend endpoints unreachable: {bad}")
    log(f"backend: REAL project {info['project_id']} (environment {cfg_backend.get('env_name', 'dev')}); accounts will use prefix {account_prefix(run_id)}")
    return BackendHandle("firebase-real", None, {}, str(log_file), now_iso(),
                         detail={"project_id": info["project_id"], "storage_bucket": info.get("storage_bucket"),
                                 "env_name": cfg_backend.get("env_name", "dev"), "account_prefix": account_prefix(run_id),
                                 "config_files": list(cfg_backend.get("config_files") or []), "health": health,
                                 "note": "real cloud project; no local process"})


def stop_backend(handle: BackendHandle | dict | None, run_id: str) -> None:
    if not handle:
        return
    h = handle if isinstance(handle, dict) else handle.to_dict()
    pid = h.get("pid")
    if pid and pid_alive(pid):
        kill_process_group(pid, grace_s=8)
    res = h.get("reservation")
    if res:
        Reservation(res, run_id, 1).release()


def backend_alive(handle: dict | None) -> bool:
    return bool(handle and handle.get("pid") and pid_alive(handle["pid"]))
