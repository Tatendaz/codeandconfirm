"""Host capacity + reservations.

Reservations are files under $CODEANDCONFIRM_HOME/locks/<resource>.lock holding the owner
(run id, pid, created_at, ttl). A lock is stale when its owner pid is dead or its ttl has
passed; stale locks are reclaimed. Nothing here ever kills a foreign process.

Resources used by the coordinator:
  functional-slot-N       one per allowed concurrent functional (device) run
  ios-device:<udid>       dedicated simulator
  android-device:<serial> dedicated emulator
  backend-ports:<p1,p2>   host ports a backend binds (Android apps often hardcode 10.0.2.2:<port>)
  desktop-focus           the single foreground desktop (only for desktop computer-use backends)
  bench-exclusive         performance measurements need the whole host
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import home_dir
from .util import now_iso, pid_alive, read_json, write_json


def locks_dir() -> Path:
    d = home_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_path(resource: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._:-]", "_", resource)
    return locks_dir() / f"{safe}.lock"


@dataclass
class LockInfo:
    resource: str
    owner_run: str
    owner_pid: int
    created_at: str
    expires_at: float
    note: str = ""

    def stale(self) -> bool:
        return (not pid_alive(self.owner_pid)) or time.time() > self.expires_at


class Reservation:
    """Advisory, crash-safe reservation with stale recovery."""

    def __init__(self, resource: str, owner_run: str, ttl_s: float, note: str = ""):
        self.resource = resource
        self.owner_run = owner_run
        self.ttl_s = ttl_s
        self.note = note
        self.path = _lock_path(resource)
        self._fh = None

    def current(self) -> LockInfo | None:
        data = read_json(self.path, None)
        if not data:
            return None
        try:
            return LockInfo(**data)
        except TypeError:
            return None

    def try_acquire(self) -> bool:
        meta = locks_dir() / ".meta"
        with open(meta, "a+") as m:
            fcntl.flock(m, fcntl.LOCK_EX)
            try:
                cur = self.current()
                if cur and not cur.stale() and cur.owner_run != self.owner_run:
                    return False
                info = LockInfo(self.resource, self.owner_run, os.getpid(), now_iso(), time.time() + self.ttl_s, self.note)
                write_json(self.path, info.__dict__)
                return True
            finally:
                fcntl.flock(m, fcntl.LOCK_UN)

    def acquire(self, wait_s: float, poll_s: float = 5.0) -> bool:
        deadline = time.monotonic() + wait_s
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)

    def renew(self) -> None:
        cur = self.current()
        if cur and cur.owner_run == self.owner_run:
            cur.expires_at = time.time() + self.ttl_s
            write_json(self.path, cur.__dict__)

    def release(self) -> None:
        cur = self.current()
        if cur and cur.owner_run == self.owner_run:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def release_all_for(run_id: str) -> list[str]:
    released = []
    for p in locks_dir().glob("*.lock"):
        data = read_json(p, None)
        if data and data.get("owner_run") == run_id:
            p.unlink(missing_ok=True)
            released.append(data.get("resource", p.stem))
    return released


def list_locks() -> list[LockInfo]:
    out = []
    for p in locks_dir().glob("*.lock"):
        data = read_json(p, None)
        if data:
            try:
                out.append(LockInfo(**data))
            except TypeError:
                pass
    return out


# --- host metrics ---------------------------------------------------------------


@dataclass
class HostMetrics:
    cpu_count: int
    load_1m: float
    mem_total_gb: float
    mem_free_percent: float
    ci_workers_busy: int
    ts: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _sysctl(name: str) -> str:
    r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
    return r.stdout.strip()


def host_metrics(ci_worker_pattern: str = "Runner.Worker") -> HostMetrics:
    cpu = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    try:
        mem_total = int(_sysctl("hw.memsize")) / (1024 ** 3)
    except ValueError:
        mem_total = 0.0
    free_pct = _memory_free_percent()
    busy = 0
    if ci_worker_pattern:
        r = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True)
        busy = sum(1 for line in r.stdout.splitlines() if ci_worker_pattern in line)
    return HostMetrics(cpu, round(load1, 2), round(mem_total, 1), free_pct, busy, now_iso())


def _memory_free_percent() -> float:
    # macOS: memory_pressure prints "System-wide memory free percentage: NN%"
    r = subprocess.run(["memory_pressure"], capture_output=True, text=True)
    m = re.search(r"free percentage:\s*(\d+)%", r.stdout)
    if m:
        return float(m.group(1))
    # fallback: vm_stat free+inactive pages
    r = subprocess.run(["vm_stat"], capture_output=True, text=True)
    pages = {}
    for line in r.stdout.splitlines():
        mm = re.match(r"(Pages (free|inactive|speculative|active|wired down)):\s+(\d+)", line)
        if mm:
            pages[mm.group(2)] = int(mm.group(3))
    total = sum(pages.values()) or 1
    return round(100.0 * (pages.get("free", 0) + pages.get("inactive", 0) + pages.get("speculative", 0)) / total, 1)


def auto_functional_slots(mem_total_gb: float) -> int:
    return max(1, int(mem_total_gb // 24))


@dataclass
class CapacityDecision:
    ok: bool
    reason: str
    metrics: HostMetrics


def capacity_check(cfg_sched: dict, *, need_exclusive: bool = False) -> CapacityDecision:
    m = host_metrics(cfg_sched.get("ci_worker_process_pattern", "Runner.Worker"))
    min_free = float(cfg_sched.get("min_free_memory_percent", 15))
    max_load = float(cfg_sched.get("max_load_per_core", 0.85)) * m.cpu_count
    if m.mem_free_percent < min_free:
        return CapacityDecision(False, f"free memory {m.mem_free_percent}% < {min_free}%", m)
    if m.load_1m > max_load:
        return CapacityDecision(False, f"load {m.load_1m} > {max_load:.1f} ({m.cpu_count} cores)", m)
    if need_exclusive and m.ci_workers_busy:
        return CapacityDecision(False, f"{m.ci_workers_busy} CI job(s) running on this host; benchmarks need exclusive access", m)
    if need_exclusive and m.load_1m > 0.35 * m.cpu_count:
        return CapacityDecision(False, f"load {m.load_1m} too high for a benchmark on {m.cpu_count} cores", m)
    return CapacityDecision(True, "ok", m)


def wait_for_capacity(cfg_sched: dict, *, need_exclusive: bool = False, should_abort=lambda: False,
                      log=lambda s: None) -> CapacityDecision:
    wait_s = float(cfg_sched.get("wait_for_capacity_minutes", 20)) * 60
    deadline = time.monotonic() + wait_s
    last = None
    while True:
        d = capacity_check(cfg_sched, need_exclusive=need_exclusive)
        if d.ok:
            return d
        if d.reason != last:
            log(f"waiting for capacity: {d.reason}")
            last = d.reason
        if should_abort() or time.monotonic() > deadline:
            return d
        time.sleep(15)


def functional_slots(cfg_sched: dict) -> int:
    n = int(cfg_sched.get("max_functional_runs", 0) or 0)
    if n > 0:
        return n
    return auto_functional_slots(host_metrics("").mem_total_gb)


def acquire_functional_slot(run_id: str, cfg_sched: dict, wait_s: float) -> Reservation | None:
    ttl = float(cfg_sched.get("lock_ttl_minutes", 180)) * 60
    deadline = time.monotonic() + wait_s
    while True:
        for i in range(functional_slots(cfg_sched)):
            r = Reservation(f"functional-slot-{i}", run_id, ttl, "functional QA run")
            if r.try_acquire():
                return r
        if time.monotonic() > deadline:
            return None
        time.sleep(5)


def ports_in_use(ports: Iterable[int]) -> dict[int, str]:
    """Return {port: 'cmd pid'} for ports that already have a LISTEN socket."""
    out = {}
    for p in ports:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{p}", "-sTCP:LISTEN"], capture_output=True, text=True)
        lines = r.stdout.strip().splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            out[p] = f"{parts[0]} pid={parts[1]}"
    return out
