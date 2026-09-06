"""Launch and supervise one Codex QA worker (`codex exec`), durably.

Each worker gets its own workspace directory containing:
  AGENTS.md      the QA role (independent tester; no product edits; no recursive reviewers)
  task.md        the concrete assignment (candidate, devices, journeys, evidence rules)
  candidate.diff the diff under review
  checkout ->    symlink to the run's isolated checkout
  evidence/      where ccdevice writes screenshots + actions.jsonl
  patches/       test-only patches the worker may propose

The worker's structured verdict is enforced through `--output-schema`; the model actually
used is verified afterwards from the Codex session rollout, never from the model's own claim.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .util import kill_process_group, now_iso, pid_alive, read_json, read_jsonl, run, which, write_json, redact

PROMPTS_DIR = Path(__file__).parent / "prompts"
KNOWN_CODEX_BINARIES = [
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
]


class CodexError(RuntimeError):
    pass


# --- binary + model resolution --------------------------------------------------


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def codex_version(binary: str) -> str | None:
    r = run([binary, "--version"], timeout=30)
    return r.stdout.strip().split()[-1] if r.ok and r.stdout.strip() else None


def codex_models(binary: str) -> list[dict]:
    r = run([binary, "debug", "models"], timeout=120)
    if not r.ok:
        return []
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return d.get("models", d) if isinstance(d, dict) else d


def resolve_codex(binary_cfg: str, model: str) -> dict:
    """Pick a Codex binary that can serve `model`. Returns a report; never substitutes the model."""
    candidates = []
    if binary_cfg:
        candidates.append(os.path.expanduser(binary_cfg))
    if which("codex"):
        candidates.append(which("codex"))
    candidates += [b for b in KNOWN_CODEX_BINARIES if Path(b).exists()]
    seen, checked = set(), []
    for b in candidates:
        if b in seen or not Path(b).exists():
            continue
        seen.add(b)
        ver = codex_version(b)
        models = codex_models(b)
        slugs = [m.get("slug") for m in models if isinstance(m, dict)]
        entry = {"binary": b, "version": ver, "models": slugs, "serves_model": model in slugs}
        m = next((x for x in models if isinstance(x, dict) and x.get("slug") == model), None)
        if m:
            entry["model_info"] = {"display_name": m.get("display_name"), "context_window": m.get("context_window"),
                                   "efforts": [e.get("effort") for e in m.get("supported_reasoning_levels", []) if isinstance(e, dict)]}
        checked.append(entry)
        if entry["serves_model"]:
            return {"ok": True, "binary": b, "version": ver, "model": model, "model_info": entry.get("model_info"), "checked": checked}
    return {"ok": False, "binary": None, "version": None, "model": model, "checked": checked,
            "reason": f"no Codex binary lists model {model!r}; run `codex update` or set [codex].binary"}


def auth_status() -> dict:
    p = codex_home() / "auth.json"
    if not p.exists():
        return {"logged_in": False, "mode": None}
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"logged_in": False, "mode": None}
    return {"logged_in": bool(d.get("tokens") or d.get("OPENAI_API_KEY")), "mode": d.get("auth_mode"), "last_refresh": d.get("last_refresh")}


def inherited_instruction_risks() -> list[str]:
    """Global Codex instruction files leak into every worker; flag ones that would revive old loops."""
    risks = []
    p = codex_home() / "AGENTS.md"
    if p.exists():
        txt = p.read_text(errors="replace").lower()
        for needle in ("coderabbit", "@coderabbitai", "review loop", "watcher"):
            if needle in txt:
                risks.append(f"{p} mentions {needle!r}; the QA role prompt overrides it, but consider removing it (see docs/migration.md)")
                break
    return risks


# --- worker ----------------------------------------------------------------------


@dataclass
class WorkerSpec:
    name: str
    workspace: Path
    prompt: str
    binary: str
    model: str
    reasoning_effort: str
    sandbox: str                      # danger-full-access | read-only | workspace-write
    timeout_s: float
    env: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class WorkerResult:
    name: str
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    started_at: str
    finished_at: str | None
    thread_id: str | None
    verdict: dict | None
    verdict_error: str = ""
    usage: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    rollout_path: str | None = None
    model_used: list[str] = field(default_factory=list)
    cli_version: str | None = None
    sandbox_policy: dict | None = None
    commands: int = 0
    mcp_calls: int = 0
    events_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class CodexWorker:
    def __init__(self, spec: WorkerSpec):
        self.spec = spec
        self.ws = spec.workspace
        self.ws.mkdir(parents=True, exist_ok=True)
        self.events_path = self.ws / "events.jsonl"
        self.last_msg_path = self.ws / "last-message.json"
        self.prompt_path = self.ws / "prompt.md"
        self.meta_path = self.ws / "worker.json"
        self.proc: subprocess.Popen | None = None

    def argv(self) -> list[str]:
        s = self.spec
        args = [s.binary, "exec", "--json", "-m", s.model,
                "-c", f'model_reasoning_effort="{s.reasoning_effort}"',
                "-c", "notify=[]", "--disable", "hooks",
                "-C", str(self.ws), "--skip-git-repo-check",
                "--output-schema", str(PROMPTS_DIR / "verdict.schema.json"),
                "-o", str(self.last_msg_path)]
        if s.sandbox == "danger-full-access":
            args.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            args += ["-s", s.sandbox]
        args += s.extra_args
        args.append("-")  # prompt on stdin
        return args

    def start(self) -> int:
        self.prompt_path.write_text(self.spec.prompt)
        env = dict(os.environ)
        env.update(self.spec.env)
        env["CODEANDCONFIRM_WORKER"] = self.spec.name
        env.pop("CLAUDECODE", None)
        argv = self.argv()
        (self.ws / "argv.txt").write_text(" ".join(argv))
        out = open(self.events_path, "ab")
        err = open(self.ws / "stderr.log", "ab")
        self.proc = subprocess.Popen(argv, cwd=str(self.ws), env=env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                     start_new_session=True)
        assert self.proc.stdin
        self.proc.stdin.write(self.spec.prompt.encode())
        self.proc.stdin.close()
        write_json(self.meta_path, {"name": self.spec.name, "pid": self.proc.pid, "started_at": now_iso(), "model": self.spec.model,
                                    "reasoning_effort": self.spec.reasoning_effort, "sandbox": self.spec.sandbox,
                                    "binary": self.spec.binary, "timeout_s": self.spec.timeout_s})
        return self.proc.pid

    def poll(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def cancel(self) -> None:
        meta = read_json(self.meta_path, {})
        pid = (self.proc.pid if self.proc else None) or meta.get("pid")
        if pid and pid_alive(pid):
            kill_process_group(pid, grace_s=8)
        meta["cancelled_at"] = now_iso()
        write_json(self.meta_path, meta)

    def wait(self, *, should_cancel=lambda: False, heartbeat=lambda: None, poll_s: float = 5.0) -> WorkerResult:
        assert self.proc is not None
        meta = read_json(self.meta_path, {})
        started = meta.get("started_at", now_iso())
        t0 = time.monotonic()
        timed_out = cancelled = False
        while self.proc.poll() is None:
            if should_cancel():
                cancelled = True; self.cancel(); break
            if time.monotonic() - t0 > self.spec.timeout_s:
                timed_out = True; self.cancel(); break
            heartbeat()
            time.sleep(poll_s)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        if not cancelled and should_cancel():
            cancelled = True   # the process was killed by a cancellation that raced our poll loop
        return self.collect(exit_code=self.proc.returncode, timed_out=timed_out, cancelled=cancelled, started_at=started)

    def collect(self, *, exit_code: int | None, timed_out: bool, cancelled: bool, started_at: str) -> WorkerResult:
        events = read_jsonl(self.events_path)
        thread_id = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        usage: dict = {}
        errors: list[str] = []
        commands = mcp = 0
        for e in events:
            t = e.get("type")
            if t == "turn.completed":
                usage = e.get("usage", usage)
            elif t in ("error", "turn.failed"):
                errors.append(redact(json.dumps(e.get("error") or e.get("message") or e)[:500]))
            elif t == "item.completed":
                it = e.get("item", {})
                if it.get("type") == "error":
                    msg = it.get("message", "")
                    if "clamping" not in msg and "skills context budget" not in msg:
                        errors.append(redact(msg[:300]))
                elif it.get("type") == "command_execution":
                    commands += 1
                elif it.get("type") == "mcp_tool_call":
                    mcp += 1
        verdict, verr = None, ""
        if self.last_msg_path.exists():
            raw = self.last_msg_path.read_text().strip()
            try:
                verdict = json.loads(raw)
                if not isinstance(verdict, dict):
                    verdict, verr = None, "last message is not a JSON object"
            except json.JSONDecodeError as ex:
                verr = f"last message is not valid JSON: {ex}"
        else:
            verr = "worker produced no final message"
        rollout = find_rollout(thread_id) if thread_id else None
        models, cli_ver, sandbox = [], None, None
        if rollout:
            models, cli_ver, sandbox = rollout_facts(rollout)
        res = WorkerResult(self.spec.name, exit_code, timed_out, cancelled, started_at, now_iso(), thread_id, verdict, verr,
                           usage, errors, str(rollout) if rollout else None, models, cli_ver, sandbox, commands, mcp,
                           str(self.events_path))
        write_json(self.ws / "result.json", res.to_dict())
        return res


def find_rollout(thread_id: str, wait_s: float = 10) -> Path | None:
    root = codex_home() / "sessions"
    deadline = time.monotonic() + wait_s
    while True:
        hits = list(root.rglob(f"*{thread_id}*.jsonl")) if root.exists() else []
        if hits:
            return hits[0]
        if time.monotonic() > deadline:
            return None
        time.sleep(1)


def rollout_facts(path: Path) -> tuple[list[str], str | None, dict | None]:
    """Models named in turn_context entries (what the API was actually asked to run), cli version, sandbox."""
    models: list[str] = []
    cli_ver = None
    sandbox = None
    for line in path.read_text(errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = d.get("payload", {})
        if d.get("type") == "session_meta":
            cli_ver = p.get("cli_version")
        if d.get("type") == "turn_context":
            m = p.get("model")
            if m and m not in models:
                models.append(m)
            sandbox = p.get("sandbox_policy", sandbox)
    return models, cli_ver, sandbox


def resume_worker(spec: WorkerSpec, thread_id: str, followup: str) -> "CodexWorker":
    """Continue an interrupted worker thread with a follow-up instruction (best effort)."""
    w = CodexWorker(spec)
    w.spec.extra_args = ["resume", thread_id]  # `codex exec resume <id>` reads the new prompt from stdin
    return w


def load_role(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()
