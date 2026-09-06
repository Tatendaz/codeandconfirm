"""Codex worker result collection (model verified from the rollout) and the durable repair counter."""

from __future__ import annotations

import json
from pathlib import Path

from codeandconfirm.codex_worker import CodexWorker, WorkerSpec, rollout_facts
from codeandconfirm.config import Config, DEFAULTS
from codeandconfirm.coordinator import Coordinator
from codeandconfirm.candidate import Candidate


def _spec(ws: Path) -> WorkerSpec:
    return WorkerSpec(name="ios", workspace=ws, prompt="p", binary="/bin/false", model="gpt-6-astra",
                      reasoning_effort="high", sandbox="read-only", timeout_s=10)


def _rollout(tmp_path: Path, thread_id: str, model: str) -> Path:
    sessions = tmp_path / "codex-home" / "sessions" / "2026" / "01" / "01"
    sessions.mkdir(parents=True)
    p = sessions / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"cli_version": "0.153.4", "originator": "codex_exec"}},
        {"type": "turn_context", "payload": {"model": model, "sandbox_policy": {"type": "read-only"}}},
        {"type": "turn_context", "payload": {"model": model}},
    ]
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return p


def test_collect_reads_verdict_usage_and_rollout_model(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    ws = tmp_path / "ws"; ws.mkdir()
    tid = "01aa-thread"
    _rollout(tmp_path, tid, "gpt-6-astra")
    events = [
        {"type": "thread.started", "thread_id": tid},
        {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"id": "2", "type": "mcp_tool_call"}},
        {"type": "item.completed", "item": {"id": "3", "type": "error", "message": "clamping SessionEnd hook timeout"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]
    (ws / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (ws / "last-message.json").write_text(json.dumps({"worker": "ios", "verdict": "PASS", "summary": "", "scenarios": [], "findings": [],
                                                       "untested": [], "blockers": [], "test_patches": [], "commands_run": [], "model_self_report": "x"}))
    w = CodexWorker(_spec(ws))
    res = w.collect(exit_code=0, timed_out=False, cancelled=False, started_at="t0")
    assert res.verdict["verdict"] == "PASS"
    assert res.model_used == ["gpt-6-astra"] and res.cli_version == "0.153.4"
    assert res.commands == 1 and res.mcp_calls == 1
    assert res.errors == []                     # the hook-clamp notice is noise, not an error
    assert res.usage["input_tokens"] == 10


def test_collect_flags_missing_or_invalid_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "events.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": "zz"}) + "\n")
    w = CodexWorker(_spec(ws))
    res = w.collect(exit_code=1, timed_out=False, cancelled=False, started_at="t0")
    assert res.verdict is None and "no final message" in res.verdict_error
    (ws / "last-message.json").write_text("not json")
    res = w.collect(exit_code=0, timed_out=False, cancelled=False, started_at="t0")
    assert res.verdict is None and "not valid JSON" in res.verdict_error
    assert res.model_used == []                 # no rollout → gate will BLOCK as unverifiable


def test_rollout_facts_reports_every_model_seen(tmp_path):
    p = _rollout(tmp_path, "t", "gpt-6-astra")
    with open(p, "a") as fh:
        fh.write(json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.5"}}) + "\n")
    models, ver, sandbox = rollout_facts(p)
    assert models == ["gpt-6-astra", "gpt-5.5"] and ver == "0.153.4" and sandbox["type"] == "read-only"


def _cand(ref: str, sha: str) -> Candidate:
    return Candidate("branch", "/repo", ref, sha, "main", "b" * 40, "b" * 40, [], "")


def test_repair_counter_is_durable_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    import copy
    cfg = Config(copy.deepcopy(DEFAULTS), [], tmp_path)
    co = Coordinator(cfg, tmp_path / "repo")

    class FakeRun:  # only .id is needed by _record_repair_outcome
        id = "run-1"
    co.run = FakeRun()
    assert co._repair_cycle_for(_cand("feat/x", "1" * 40)) == 0
    co._record_repair_outcome({"candidate_ref": "feat/x", "candidate_sha": "1" * 40}, "FAIL")
    # same SHA again (re-run without a fix) does not consume a cycle
    assert co._repair_cycle_for(_cand("feat/x", "1" * 40)) == 0
    # a new SHA after a FAIL is repair cycle 1 — even from a brand-new Coordinator (new session)
    co2 = Coordinator(cfg, tmp_path / "repo"); co2.run = FakeRun()
    assert co2._repair_cycle_for(_cand("feat/x", "2" * 40)) == 1
    co2._record_repair_outcome({"candidate_ref": "feat/x", "candidate_sha": "2" * 40}, "FAIL")
    assert Coordinator(cfg, tmp_path / "repo")._repair_cycle_for(_cand("feat/x", "3" * 40)) == 2
    # PASS resets the counter
    co3 = Coordinator(cfg, tmp_path / "repo"); co3.run = FakeRun()
    co3._record_repair_outcome({"candidate_ref": "feat/x", "candidate_sha": "3" * 40}, "PASS")
    assert Coordinator(cfg, tmp_path / "repo")._repair_cycle_for(_cand("feat/x", "4" * 40)) == 0
    # explicit reset
    co4 = Coordinator(cfg, tmp_path / "repo"); co4.run = FakeRun()
    co4._record_repair_outcome({"candidate_ref": "feat/y", "candidate_sha": "5" * 40}, "FAIL")
    assert Coordinator(cfg, tmp_path / "repo")._repair_cycle_for(_cand("feat/y", "6" * 40), reset=True) == 0


def test_android_test_friendly_resets_screen_and_foreground(monkeypatch):
    from codeandconfirm.device.android_adb import AndroidEmulator
    calls: list[tuple] = []

    class Fake(AndroidEmulator):
        def __init__(self):  # no adb, no device
            pass
        def _shell(self, *args, check=True, **kw):
            calls.append(args); return ""
    Fake().set_test_friendly()
    flat = [" ".join(c) for c in calls]
    assert "input keyevent KEYCODE_WAKEUP" in flat and "svc power stayon true" in flat and "wm dismiss-keyguard" in flat
    assert "am force-stop com.android.settings" in flat
    assert flat[-1] == "input keyevent KEYCODE_HOME"                 # foreign apps gone, home screen last
    assert sum("animation_scale" in f or "animator_duration_scale" in f for f in flat) == 3


def test_resume_reruns_only_workers_without_a_final_verdict():
    from codeandconfirm.coordinator import worker_needs_run
    assert worker_needs_run(None)
    assert worker_needs_run({"verdict": None})
    assert worker_needs_run({"verdict": {"verdict": "BLOCKED"}})       # coverage not obtained → try again
    assert not worker_needs_run({"verdict": {"verdict": "PASS"}})
    assert not worker_needs_run({"verdict": {"verdict": "FAIL"}})
