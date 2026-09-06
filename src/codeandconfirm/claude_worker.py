"""Launch and supervise one Claude Code QA worker (`claude -p`), durably — the same contract as
`codex_worker.CodexWorker`, so `[roles].qa = "claude"` swaps the engine without touching the coordinator.

The workspace layout is identical (AGENTS.md role, task.md, candidate.diff, checkout symlink, evidence/,
patches/). The role is appended to the system prompt, the task is the prompt, the verdict is enforced with
`--json-schema` and read from the result's `structured_output`, and the model actually used is verified
afterwards from the session transcript (`~/.claude/projects/<cwd-slug>/<session-id>.jsonl`: the `model`
field of every assistant message) — never from the model's own claim and not from `modelUsage`, which also
lists the CLI's helper models.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

from .codex_worker import CodexWorker, WorkerResult, WorkerSpec, PROMPTS_DIR
from .util import now_iso, read_json, redact, run, which, write_json

READ_ONLY_TOOLS = "Read,Grep,Glob"


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def claude_version(binary: str) -> str | None:
    r = run([binary, "--version"], timeout=30)
    return r.stdout.strip().split()[0] if r.ok and r.stdout.strip() else None


def resolve_claude(binary_cfg: str, model: str) -> dict:
    """Pick the Claude Code binary. The model cannot be listed offline; it is verified per worker from the
    transcript, so this only checks the binary exists and answers `--version`."""
    cands = [os.path.expanduser(binary_cfg)] if binary_cfg else []
    if which("claude"):
        cands.append(which("claude"))
    for b in cands:
        if b and Path(b).exists():
            ver = claude_version(b)
            if ver:
                return {"ok": True, "binary": b, "version": ver, "model": model, "engine": "claude"}
    return {"ok": False, "binary": None, "version": None, "model": model, "engine": "claude",
            "reason": "no `claude` binary found; install Claude Code or set [claude].binary"}


def model_matches(requested: str, used: str) -> bool:
    """Exact id, or the dated variant of the same id (`claude-opus-5` vs `claude-opus-5-20260601`)."""
    return used == requested or used.startswith(requested + "-")


class ClaudeWorker(CodexWorker):
    """Same lifecycle as CodexWorker; only the command line and the result parsing differ."""

    def __init__(self, spec: WorkerSpec):
        super().__init__(spec)
        self.result_path = self.ws / "result-raw.json"
        self.session_id = read_json(self.meta_path, {}).get("session_id") or str(uuid.uuid4())

    def argv(self) -> list[str]:
        s = self.spec
        schema_d = json.loads((PROMPTS_DIR / "verdict.schema.json").read_text())
        schema_d.pop("$schema", None)                     # the CLI's validator rejects the 2020-12 meta-schema URI
        schema = json.dumps(schema_d, separators=(",", ":"))
        args = [s.binary, "-p", "--output-format", "json", "--json-schema", schema, "--model", s.model,
                "--session-id", self.session_id,
                "--append-system-prompt-file", str(self.ws / "AGENTS.md"),
                "--setting-sources", "project"]           # the user's global settings/hooks are another agent's workflow
        if s.reasoning_effort:
            args += ["--effort", s.reasoning_effort]
        for d in ("checkout", "evidence"):
            p = self.ws / d
            if p.exists():
                args += ["--add-dir", str(p.resolve())]
        if s.sandbox == "danger-full-access":
            args.append("--dangerously-skip-permissions")
        else:
            args += ["--tools", READ_ONLY_TOOLS, "--permission-mode", "dontAsk"]
        args += s.extra_args
        return args

    def start(self) -> int:
        self.prompt_path.write_text(self.spec.prompt)
        env = dict(os.environ)
        env.update(self.spec.env)
        env["CODEANDCONFIRM_WORKER"] = self.spec.name
        env.pop("CLAUDECODE", None)                       # allow launching from inside a Claude Code session
        argv = self.argv()
        (self.ws / "argv.txt").write_text(" ".join(a if len(a) < 200 else "<schema>" for a in argv))
        out = open(self.result_path, "wb")
        err = open(self.ws / "stderr.log", "ab")
        self.proc = subprocess.Popen(argv, cwd=str(self.ws), env=env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                     start_new_session=True)
        assert self.proc.stdin
        self.proc.stdin.write(self.spec.prompt.encode())
        self.proc.stdin.close()
        write_json(self.meta_path, {"name": self.spec.name, "pid": self.proc.pid, "started_at": now_iso(), "model": self.spec.model,
                                    "reasoning_effort": self.spec.reasoning_effort, "sandbox": self.spec.sandbox, "binary": self.spec.binary,
                                    "timeout_s": self.spec.timeout_s, "engine": "claude", "session_id": self.session_id})
        return self.proc.pid

    def collect(self, *, exit_code: int | None, timed_out: bool, cancelled: bool, started_at: str) -> WorkerResult:
        raw = self.result_path.read_text().strip() if self.result_path.exists() else ""
        result: dict = {}
        verdict, verr = None, ""
        errors: list[str] = []
        if raw:
            try:
                result = json.loads(raw)
                if not isinstance(result, dict):
                    result, verr = {}, "claude printed a non-object result"
            except json.JSONDecodeError as ex:
                verr = f"claude output is not valid JSON: {ex}"
        else:
            verr = "worker produced no result"
        if result:
            if result.get("is_error"):
                errors.append(redact(str(result.get("result") or result.get("subtype") or "error")[:500]))
            so = result.get("structured_output")
            if isinstance(so, dict):
                verdict = so
            else:
                # no structured output (e.g. max turns hit): try the plain result text as JSON, else fail loudly
                try:
                    cand = json.loads(result.get("result") or "")
                    verdict = cand if isinstance(cand, dict) else None
                except (json.JSONDecodeError, TypeError):
                    verdict = None
                if verdict is None:
                    verr = verr or f"no structured verdict in the result (subtype {result.get('subtype')})"
        usage = dict(result.get("usage") or {})
        usage.setdefault("input_tokens", usage.get("input_tokens"))
        usage["total_cost_usd"] = result.get("total_cost_usd")
        usage["num_turns"] = result.get("num_turns")
        session_id = result.get("session_id") or self.session_id
        transcript = find_transcript(session_id)
        models, commands, tool_calls = ([], 0, 0)
        if transcript:
            models, commands, tool_calls = transcript_facts(transcript)
        res = WorkerResult(self.spec.name, exit_code, timed_out, cancelled, started_at, now_iso(), session_id, verdict, verr,
                           usage, errors, str(transcript) if transcript else None, models, claude_version(self.spec.binary),
                           {"type": self.spec.sandbox}, commands, tool_calls, str(self.result_path))
        write_json(self.ws / "result.json", res.to_dict())
        return res


def find_transcript(session_id: str, wait_s: float = 10) -> Path | None:
    import time
    root = claude_home() / "projects"
    deadline = time.monotonic() + wait_s
    while True:
        hits = list(root.rglob(f"{session_id}.jsonl")) if root.exists() else []
        if hits:
            return hits[0]
        if time.monotonic() > deadline:
            return None
        time.sleep(1)


def transcript_facts(path: Path) -> tuple[list[str], int, int]:
    """Models named by assistant messages (what the API actually answered with), Bash calls, other tool calls."""
    models: list[str] = []
    commands = tools = 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        m = msg.get("model")
        if m and m not in models:
            models.append(m)
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("name") == "Bash":
                    commands += 1
                else:
                    tools += 1
    return models, commands, tools
