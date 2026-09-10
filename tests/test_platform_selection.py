"""Platform selection from the diff (`qa.platforms_from_diff`): a run tests only the platforms the change can
affect, and never silently: the choice is recorded for the report and named in the gate table."""

from __future__ import annotations

import copy
from pathlib import Path

from codeandconfirm.codex_worker import PROMPTS_DIR
from codeandconfirm.config import Config, DEFAULTS
from codeandconfirm.coordinator import Coordinator, ReviewOptions
from codeandconfirm.gate import evaluate
from codeandconfirm.report import render_markdown
from test_gate import CAND, good_ctx, make_cfg


def _co(tmp_path: Path, from_diff: bool = True, platforms=("ios", "android"), configured=("ios", "android")) -> Coordinator:
    data = copy.deepcopy(DEFAULTS)
    data["qa"]["platforms"] = list(platforms)
    data["qa"]["platforms_from_diff"] = from_diff
    data["platforms"] = {p: {"bundle_id": "x"} for p in configured}
    return Coordinator(Config(data, [], tmp_path), tmp_path / "repo", log=lambda s: None)


def _cand(*files: str) -> dict:
    return {"candidate_sha": "a" * 40, "changed_files": list(files)}


def test_diff_selects_only_the_touched_platform(tmp_path):
    co = _co(tmp_path)
    assert co._select_platforms(_cand("ios/App/Views/Auth.swift"), ReviewOptions()) == ["ios"]
    sel = co.ctx["platform_selection"]
    assert sel["mode"] == "diff" and sel["skipped"] == ["android"] and "android not tested" in sel["reason"]
    assert co._select_platforms(_cand("android/app/src/main/java/x/Trees.kt"), ReviewOptions()) == ["android"]


def test_shared_backend_code_selects_every_platform(tmp_path):
    co = _co(tmp_path)
    assert co._select_platforms(_cand("firebase/firestore.rules", "docs/x.md"), ReviewOptions()) == ["ios", "android"]
    assert co.ctx["platform_selection"]["mode"] == "diff" and co.ctx["platform_selection"]["skipped"] == []


def test_a_diff_touching_no_platform_keeps_every_configured_platform(tmp_path):
    co = _co(tmp_path)
    assert co._select_platforms(_cand("docs/features/x.md", ".github/workflows/ci.yml", "ios/AppUITests/T.swift"), ReviewOptions()) == ["ios", "android"]
    assert co.ctx["platform_selection"]["mode"] == "diff-fallback"
    assert co.ctx["platform_selection"]["skipped"] == []


def test_command_line_platforms_override_the_diff(tmp_path):
    co = _co(tmp_path)
    assert co._select_platforms(_cand("ios/App/A.swift"), ReviewOptions(platforms=["android"])) == ["android"]
    assert co.ctx["platform_selection"]["mode"] == "cli"


def test_flag_off_keeps_the_configured_platforms(tmp_path):
    co = _co(tmp_path, from_diff=False)
    assert co._select_platforms(_cand("ios/App/A.swift"), ReviewOptions()) == ["ios", "android"]
    assert co.ctx["platform_selection"]["mode"] == "configured"


def test_a_platform_without_a_platforms_table_is_never_selected(tmp_path):
    co = _co(tmp_path, configured=("ios",))
    assert co._select_platforms(_cand("android/app/src/main/java/x/T.kt"), ReviewOptions()) == ["ios"]
    assert co.ctx["platform_selection"]["mode"] == "diff-fallback"


def test_the_selection_drives_every_later_phase(tmp_path):
    co = _co(tmp_path)
    assert co._platforms(ReviewOptions()) == ["ios", "android"]          # before execute(): the configured list
    co._selected_platforms = co._select_platforms(_cand("ios/App/A.swift"), ReviewOptions())
    assert co._platforms(ReviewOptions()) == ["ios"]
    co.ctx["platform_status"] = {}
    assert co._active_platforms(ReviewOptions()) == ["ios"]


def test_gate_and_report_name_the_platform_that_was_not_tested(tmp_path):
    cfg = make_cfg(tmp_path)
    sel = {"mode": "diff", "selected": ["ios"], "skipped": ["android"],
           "reason": "qa.platforms_from_diff: the diff changes product code for ios; android not tested (no product code changed there)"}
    ctx = good_ctx(tmp_path, platform_selection=sel)
    g = evaluate(cfg, CAND, ctx)
    assert g.verdict == "PASS", [c for c in g.checks if not c.ok]
    c = next(c for c in g.checks if c.name == "platforms.not-tested")
    assert c.ok and not c.required and c.outcome == "info" and "android not tested" in c.detail
    md = render_markdown({"run_id": "r", "created_at": "t"}, CAND, cfg.data, ctx, g, tmp_path)
    assert "- **Platforms:** ios · qa.platforms_from_diff" in md and "`platforms.not-tested` (advisory)" in md


def test_gate_says_nothing_when_every_platform_ran(tmp_path):
    cfg = make_cfg(tmp_path)
    for ctx in (good_ctx(tmp_path / "a"), good_ctx(tmp_path / "b", platform_selection={"mode": "diff", "selected": ["ios"], "skipped": [], "reason": "r"})):
        g = evaluate(cfg, CAND, ctx)
        assert g.verdict == "PASS" and not any(c.name == "platforms.not-tested" for c in g.checks)
    md = render_markdown({"run_id": "r", "created_at": "t"}, CAND, cfg.data, good_ctx(tmp_path / "c"), evaluate(cfg, CAND, good_ctx(tmp_path / "d")), tmp_path)
    assert "**Platforms:**" not in md


def test_role_prompt_tells_the_worker_to_read_less():
    role = (PROMPTS_DIR / "qa_role.md").read_text()
    assert "Read the tree after each action" not in role
    assert "tree --grep <text>" in role and "do\n  not open the image files" in role and "Work economically" in role
    # the evidence rules are untouched: screenshots are still required
    assert "Take a screenshot at every meaningful state" in role and "Evidence or it did not happen" in role
