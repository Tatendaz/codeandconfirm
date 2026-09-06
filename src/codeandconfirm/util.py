"""Small shared helpers: subprocess with true exit codes, JSON I/O, redaction, hashing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# --- time -------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def monotonic() -> float:
    return time.monotonic()


# --- redaction --------------------------------------------------------------

# Values that look like credentials. Kept deliberately broad: a false positive costs
# a few masked characters in a log; a false negative leaks a token into an artifact.
_SECRET_VALUE_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),           # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                # OpenAI-style keys
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),              # Google API keys
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),         # Slack
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.=]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
_SECRET_ENV_KEY = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key|credential|auth)")


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _SECRET_VALUE_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def redact_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: ("[REDACTED]" if _SECRET_ENV_KEY.search(k) else v) for k, v in env.items()}


# --- files ------------------------------------------------------------------


_WRITE_SEQ = 0


def atomic_write_text(path: Path, text: str) -> None:
    global _WRITE_SEQ
    import threading
    path.parent.mkdir(parents=True, exist_ok=True)
    _WRITE_SEQ += 1
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}-{threading.get_ident()}-{_WRITE_SEQ}")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(Path(path), json.dumps(data, indent=2, sort_keys=False, default=str) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def expand(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


# --- subprocess ---------------------------------------------------------------


@dataclass
class CmdResult:
    argv: list[str]
    cwd: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: str
    duration_s: float
    timed_out: bool = False
    pid: int | None = None
    log_file: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_record(self, max_chars: int = 4000) -> dict:
        d = asdict(self)
        d["stdout"] = redact(self.stdout[-max_chars:])
        d["stderr"] = redact(self.stderr[-max_chars:])
        return d


def _shell_argv(cmd: str | Iterable[str]) -> list[str]:
    if isinstance(cmd, str):
        return ["/bin/bash", "-lc", cmd]
    return list(cmd)


def run(
    cmd: str | Iterable[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    log_file: str | Path | None = None,
    check: bool = False,
    input_text: str | None = None,
    record_to: Path | None = None,
    label: str | None = None,
) -> CmdResult:
    """Run a command, preserving the real exit code and (optionally) teeing output to a log.

    Commands are run in their own process group so a timeout or cancellation can kill
    the whole tree (gradle daemons, xcodebuild children) rather than only the shell.
    """
    argv = _shell_argv(cmd)
    full_env = dict(os.environ)
    if env:
        full_env.update({k: str(v) for k, v in env.items()})
    started = now_iso()
    t0 = time.monotonic()
    lf = None
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        lf = open(log_file, "ab")
        lf.write(f"# {started} $ {' '.join(shlex.quote(a) for a in argv)}\n".encode())
        lf.flush()
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    LIVE_PIDS.add(proc.pid)
    timed_out = False
    try:
        out_b, err_b = proc.communicate(input=input_text.encode() if input_text is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_group(proc.pid)
        try:
            out_b, err_b = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out_b, err_b = b"", b""
    finally:
        LIVE_PIDS.discard(proc.pid)
    out = out_b.decode(errors="replace")
    err = err_b.decode(errors="replace")
    if lf:
        lf.write(out.encode())
        if err:
            lf.write(b"\n--- stderr ---\n" + err.encode())
        lf.write(f"\n# exit={proc.returncode} timed_out={timed_out}\n".encode())
        lf.close()
    res = CmdResult(
        argv=argv, cwd=str(cwd) if cwd else None, exit_code=proc.returncode, stdout=out, stderr=err,
        started_at=started, duration_s=round(time.monotonic() - t0, 3), timed_out=timed_out, pid=proc.pid,
        log_file=str(log_file) if log_file else None,
    )
    if record_to is not None:
        rec = res.to_record()
        rec["label"] = label
        append_jsonl(record_to, rec)
    if check and not res.ok:
        raise CommandFailed(res)
    return res


LIVE_PIDS: set[int] = set()


def kill_all_live() -> None:
    """Kill every child process group started through run() that is still alive."""
    for pid in list(LIVE_PIDS):
        kill_process_group(pid, grace_s=3)


class CommandFailed(RuntimeError):
    def __init__(self, result: CmdResult):
        self.result = result
        super().__init__(f"command failed (exit={result.exit_code}, timed_out={result.timed_out}): {' '.join(result.argv)[:200]}\n{redact(result.stderr[-2000:])}")


def kill_process_group(pid: int, grace_s: float = 5.0) -> None:
    """SIGTERM the process group, then SIGKILL survivors."""
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # zombie check
    try:
        with open(f"/proc/{pid}/status") as fh:  # linux only; macOS falls through
            return "zombie" not in fh.read().lower()
    except OSError:
        pass
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def which(name: str) -> str | None:
    from shutil import which as _which

    return _which(name)


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"
