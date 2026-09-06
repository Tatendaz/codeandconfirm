"""Claude as the QA engine ([roles].qa = "claude"): same role, same schema, model verified from the transcript."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from codeandconfirm.claude_worker import ClaudeWorker, model_matches, transcript_facts, READ_ONLY_TOOLS
from codeandconfirm.codex_worker import WorkerSpec
from codeandconfirm.config import Config, DEFAULTS
from codeandconfirm.gate import evaluate, qa_model_requested


def _spec(ws: Path, sandbox="danger-full-access", effort="medium") -> WorkerSpec:
    return WorkerSpec("ios", ws, "do qa", "/usr/bin/true", "claude-opus-5", effort, sandbox, 60)


def _transcript(home: Path, sid: str, model: str, extra_model: str | None = None) -> Path:
    d = home / "projects" / "-some-cwd"; d.mkdir(parents=True)
    p = d / f"{sid}.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "task"}},
        {"type": "assistant", "message": {"model": model, "content": [{"type": "tool_use", "name": "Bash", "input": {}},
                                                                     {"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "assistant", "message": {"model": extra_model or model, "content": [{"type": "text", "text": "done"}]}},
    ]
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return p


def test_argv_trusted_vs_untrusted(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir(); (ws / "AGENTS.md").write_text("role")
    (ws / "evidence").mkdir()
    a = ClaudeWorker(_spec(ws)).argv()
    assert a[:3] == ["/usr/bin/true", "-p", "--output-format"] and "json" in a
    assert "--json-schema" in a and json.loads(a[a.index("--json-schema") + 1])["title"].startswith("CodeAndConfirm")
    assert a[a.index("--model") + 1] == "claude-opus-5" and a[a.index("--effort") + 1] == "medium"
    assert "--dangerously-skip-permissions" in a and "--tools" not in a
    assert a[a.index("--append-system-prompt-file") + 1].endswith("AGENTS.md")
    assert a[a.index("--setting-sources") + 1] == "project"       # the user's global hooks/settings stay out
    assert str((ws / "evidence").resolve()) in a                     # --add-dir for evidence
    b = ClaudeWorker(_spec(ws, sandbox="read-only")).argv()
    assert "--dangerously-skip-permissions" not in b
    assert b[b.index("--tools") + 1] == READ_ONLY_TOOLS and b[b.index("--permission-mode") + 1] == "dontAsk"


def test_collect_reads_structured_verdict_and_transcript_model(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    ws = tmp_path / "ws"; ws.mkdir()
    w = ClaudeWorker(_spec(ws))
    sid = w.session_id
    _transcript(tmp_path / "claude-home", sid, "claude-opus-5")
    verdict = {"worker": "ios", "verdict": "PASS", "summary": "", "scenarios": [], "findings": [], "untested": [], "blockers": [],
               "test_patches": [], "commands_run": [], "model_self_report": "Opus"}
    (ws / "result-raw.json").write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                                                    "num_turns": 7, "total_cost_usd": 0.42, "usage": {"input_tokens": 100, "output_tokens": 9},
                                                    "modelUsage": {"claude-haiku-4-5-20251001": {}, "claude-opus-5": {}},
                                                    "structured_output": verdict, "result": json.dumps(verdict)}))
    res = w.collect(exit_code=0, timed_out=False, cancelled=False, started_at="t0")
    assert res.verdict["verdict"] == "PASS" and res.thread_id == sid
    assert res.model_used == ["claude-opus-5"]                      # from assistant messages, not modelUsage's helper model
    assert res.commands == 1 and res.mcp_calls == 1
    assert res.usage["total_cost_usd"] == 0.42 and res.usage["num_turns"] == 7 and res.usage["input_tokens"] == 100
    assert res.rollout_path and res.rollout_path.endswith(f"{sid}.jsonl")
    assert json.loads((ws / "result.json").read_text())["verdict"]["verdict"] == "PASS"


def test_collect_flags_missing_structured_output_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    ws = tmp_path / "ws"; ws.mkdir()
    w = ClaudeWorker(_spec(ws))
    res = w.collect(exit_code=1, timed_out=False, cancelled=False, started_at="t0")
    assert res.verdict is None and "no result" in res.verdict_error and res.model_used == []
    (ws / "result-raw.json").write_text(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "hit max turns",
                                                    "session_id": w.session_id}))
    res = w.collect(exit_code=0, timed_out=False, cancelled=False, started_at="t0")
    assert res.verdict is None and "no structured verdict" in res.verdict_error and res.errors == ["hit max turns"]


def test_model_matching_accepts_dated_variant_only():
    assert model_matches("claude-opus-5", "claude-opus-5")
    assert model_matches("claude-opus-5", "claude-opus-5-20260601")
    assert not model_matches("claude-opus-5", "claude-sonnet-5")
    assert not model_matches("claude-opus-5", "claude-opus-5x")
    models, cmds, tools = transcript_facts(Path(__file__))            # not a transcript → nothing, no crash
    assert models == [] and cmds == tools == 0


def test_gate_verifies_the_claude_model_when_claude_is_qa(tmp_path):
    from test_gate import make_cfg, good_ctx, CAND
    cfg = make_cfg(tmp_path, roles={"qa": "claude"}, claude={"model": "claude-opus-5"})
    assert qa_model_requested(cfg) == "claude-opus-5"
    ctx = good_ctx(tmp_path)
    ctx["workers"]["ios"]["model_used"] = ["gpt-6-astra"]              # a Codex model when Claude was configured → BLOCKED
    g = evaluate(cfg, CAND, ctx)
    assert g.verdict == "BLOCKED" and any("model-verified" in r for r in g.block_reasons)
    ctx["workers"]["ios"]["model_used"] = ["claude-opus-5-20260601"]
    assert evaluate(cfg, CAND, ctx).verdict == "PASS"
    # default roles: Codex is QA and its model is what counts
    assert qa_model_requested(Config(copy.deepcopy(DEFAULTS), [], tmp_path)) == "gpt-6-astra"
