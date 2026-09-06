"""Durable run state. Everything a resumed session needs lives under $CODEANDCONFIRM_HOME/runs/<id>/.

Layout:
  run.json            state machine, phases, pids, timestamps, repair count
  candidate.json      candidate/base SHAs, changed files, acceptance criteria, source (branch|pr)
  checkout/           isolated git worktree at the exact candidate SHA
  artifacts/          build logs, suite results, backend logs, command log (commands.jsonl)
  evidence/<worker>/  screenshots, actions.jsonl, worker verdicts
  codex/<worker>/     prompt, events.jsonl, last-message, rollout reference
  report.md/.json     final report
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import home_dir
from .util import read_json, write_json, append_jsonl, now_iso, pid_alive

VERDICTS = ("PASS", "FAIL", "BLOCKED", "CANCELLED")
PHASES = ("created", "reserving", "checkout", "backend", "build", "install", "suites", "qa", "perf", "gate", "done")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def runs_dir() -> Path:
    d = home_dir() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Phase:
    name: str
    status: str = "pending"          # pending|running|ok|failed|skipped|blocked
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class RunState:
    run_id: str
    repo_root: str
    created_at: str
    updated_at: str
    status: str = "created"          # created|running|done|cancelled
    verdict: str | None = None       # PASS|FAIL|BLOCKED|CANCELLED once done
    verdict_reason: str = ""
    current_phase: str = "created"
    phases: dict[str, dict] = field(default_factory=dict)
    repair_cycle: int = 0            # durable; never reset by a restarted session
    lineage: str | None = None       # run id of the previous cycle for the same branch
    coordinator_pid: int | None = None
    cancel_requested: bool = False
    owned: dict = field(default_factory=dict)   # resources this run created and must clean up
    config_sources: list[str] = field(default_factory=list)
    model_requested: str | None = None
    model_used: str | None = None
    notes: list[str] = field(default_factory=list)
    profile: str | None = None       # config profile the run was created with (pr | full | custom)
    qa_engine: str | None = None     # codex | claude — which CLI ran the QA workers


class Run:
    def __init__(self, run_id: str, create: bool = False, repo_root: Path | None = None):
        self.id = run_id
        self.dir = runs_dir() / run_id
        self.state_path = self.dir / "run.json"
        if create:
            if self.dir.exists():
                raise FileExistsError(self.dir)
            for sub in ("artifacts", "evidence", "codex", "pids"):
                (self.dir / sub).mkdir(parents=True, exist_ok=True)
            st = RunState(run_id=run_id, repo_root=str(repo_root or ""), created_at=now_iso(), updated_at=now_iso(),
                          coordinator_pid=os.getpid())
            for p in PHASES:
                st.phases[p] = asdict(Phase(p))
            self.state = st
            self.save()
        else:
            if not self.state_path.exists():
                raise FileNotFoundError(f"no such run: {run_id}")
            self.state = RunState(**read_json(self.state_path))

    # --- paths -------------------------------------------------------------
    @property
    def checkout_dir(self) -> Path: return self.dir / "checkout"
    @property
    def base_checkout_dir(self) -> Path: return self.dir / "base-checkout"
    @property
    def artifacts_dir(self) -> Path: return self.dir / "artifacts"
    @property
    def evidence_dir(self) -> Path: return self.dir / "evidence"
    @property
    def codex_dir(self) -> Path: return self.dir / "codex"
    @property
    def commands_log(self) -> Path: return self.artifacts_dir / "commands.jsonl"
    @property
    def events_log(self) -> Path: return self.dir / "events.jsonl"
    @property
    def candidate_path(self) -> Path: return self.dir / "candidate.json"

    # --- persistence -------------------------------------------------------
    def save(self) -> None:
        self.state.updated_at = now_iso()
        write_json(self.state_path, asdict(self.state))

    def reload(self) -> None:
        self.state = RunState(**read_json(self.state_path))

    def event(self, kind: str, **data: Any) -> None:
        append_jsonl(self.events_log, {"ts": now_iso(), "kind": kind, **data})

    def note(self, text: str) -> None:
        self.state.notes.append(f"{now_iso()} {text}")
        self.save()

    # --- phases --------------------------------------------------------------
    def phase_start(self, name: str, **detail: Any) -> None:
        ph = self.state.phases[name]
        ph.update(status="running", started_at=now_iso(), pid=os.getpid())
        ph["detail"].update(detail)
        self.state.current_phase = name
        self.state.status = "running"
        self.save(); self.event("phase_start", phase=name, **detail)

    def phase_end(self, name: str, status: str, **detail: Any) -> None:
        ph = self.state.phases[name]
        ph.update(status=status, finished_at=now_iso())
        ph["detail"].update(detail)
        self.save(); self.event("phase_end", phase=name, status=status, **{k: v for k, v in detail.items() if k != "stdout"})

    def phase_status(self, name: str) -> str:
        return self.state.phases.get(name, {}).get("status", "pending")

    def finish(self, verdict: str, reason: str = "") -> None:
        assert verdict in VERDICTS
        self.state.verdict = verdict
        self.state.verdict_reason = reason
        self.state.status = "cancelled" if verdict == "CANCELLED" else "done"
        self.state.current_phase = "done"
        self.state.phases["done"].update(status="ok", finished_at=now_iso())
        self.save(); self.event("finish", verdict=verdict, reason=reason)

    # --- ownership -----------------------------------------------------------
    def own(self, kind: str, ident: str, **meta: Any) -> None:
        self.state.owned.setdefault(kind, {})[ident] = {"since": now_iso(), **meta}
        self.save()

    def disown(self, kind: str, ident: str) -> None:
        self.state.owned.get(kind, {}).pop(ident, None)
        self.save()

    def record_pid(self, name: str, pid: int) -> None:
        (self.dir / "pids" / f"{name}.pid").write_text(str(pid))

    def pids(self) -> dict[str, int]:
        out = {}
        for f in (self.dir / "pids").glob("*.pid"):
            try:
                out[f.stem] = int(f.read_text().strip())
            except ValueError:
                pass
        return out

    def coordinator_alive(self) -> bool:
        return pid_alive(self.state.coordinator_pid)

    def request_cancel(self) -> None:
        self.state.cancel_requested = True
        self.save(); self.event("cancel_requested")

    def cancel_requested(self) -> bool:
        self.reload()
        return self.state.cancel_requested

    # --- candidate -----------------------------------------------------------
    def save_candidate(self, cand: dict) -> None:
        write_json(self.candidate_path, cand)

    def candidate(self) -> dict:
        return read_json(self.candidate_path, {})


def list_runs(limit: int = 50) -> list[RunState]:
    out = []
    for d in sorted(runs_dir().iterdir(), reverse=True):
        p = d / "run.json"
        if p.exists():
            try:
                out.append(RunState(**read_json(p)))
            except TypeError:
                continue
        if len(out) >= limit:
            break
    return out


def latest_run_for(repo_root: Path, candidate_sha: str | None = None) -> RunState | None:
    for st in list_runs(200):
        if Path(st.repo_root) != Path(repo_root):
            continue
        if candidate_sha:
            cand = read_json(runs_dir() / st.run_id / "candidate.json", {})
            if cand.get("candidate_sha") != candidate_sha:
                continue
        return st
    return None
