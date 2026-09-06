"""Real backend (development project only): project allow-list, dev-only credentials, cleanup verification."""

from __future__ import annotations

import json
import plistlib
import stat
from pathlib import Path

import pytest

from codeandconfirm import firebase_real as fr
from codeandconfirm.gate import evaluate
from codeandconfirm.report import render_markdown
from codeandconfirm.gate import GateResult


def _plist(path: Path, project: str, bucket: str = "b.appspot.com") -> None:
    with open(path, "wb") as fh:
        plistlib.dump({"PROJECT_ID": project, "STORAGE_BUCKET": bucket, "API_KEY": "AIzaFAKE"}, fh)


def _gsjson(path: Path, project: str) -> None:
    path.write_text(json.dumps({"project_info": {"project_id": project, "storage_bucket": f"{project}.appspot.com"}}))


def _checkout(tmp_path: Path, ios_project="my-app-dev", android_project="my-app-dev") -> Path:
    co = tmp_path / "checkout"
    (co / "ios").mkdir(parents=True); (co / "android").mkdir()
    _plist(co / "ios" / "GoogleService-Info.plist", ios_project)
    _gsjson(co / "android" / "google-services.json", android_project)
    return co


FILES = ["ios/GoogleService-Info.plist", "android/google-services.json"]


def test_reads_project_id_from_plist_and_json(tmp_path):
    co = _checkout(tmp_path)
    assert fr.read_client_config(co / FILES[0])["project_id"] == "my-app-dev"
    assert fr.read_client_config(co / FILES[1])["project_id"] == "my-app-dev"
    assert fr.read_client_config(co / "nope.plist") == {}


def test_verify_project_accepts_only_allow_listed_dev_project(tmp_path):
    co = _checkout(tmp_path)
    info = fr.verify_project(co, FILES, ["my-app-dev"])
    assert info["project_id"] == "my-app-dev" and info["storage_bucket"]
    with pytest.raises(fr.RealBackendRefused, match="not in backend.allowed_project_ids"):
        fr.verify_project(_checkout(tmp_path / "t", "my-app-test", "my-app-test"), FILES, ["my-app-dev"])
    with pytest.raises(fr.RealBackendRefused, match="empty"):
        fr.verify_project(co, FILES, [])
    with pytest.raises(fr.RealBackendRefused, match="disagree"):
        fr.verify_project(_checkout(tmp_path / "d", "my-app-dev", "my-app-test"), FILES, ["my-app-dev", "my-app-test"])
    with pytest.raises(fr.RealBackendRefused, match="could not read"):
        fr.verify_project(co, ["ios/missing.plist"], ["my-app-dev"])


def test_swap_env_runs_script_and_restores_on_refusal(tmp_path):
    co = _checkout(tmp_path, "demo-project", "demo-project")
    (co / "scripts").mkdir()
    script = co / "scripts" / "use-firebase-env.sh"
    # a fake swap script: "dev" writes the real configs from firebase/config/dev, "emulator" restores demo ones
    script.write_text("#!/bin/bash\nset -e\ncase \"$1\" in\n"
                      " dev) cp firebase/config/dev/GoogleService-Info.plist ios/; cp firebase/config/dev/google-services.json android/; echo swapped;;\n"
                      " emulator) echo restored > restored.txt;;\n esac\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    src = tmp_path / "real-configs"; src.mkdir()
    _plist(src / "GoogleService-Info.plist", "my-app-test"); _gsjson(src / "google-services.json", "my-app-test")
    cfg = {"env_script": "scripts/use-firebase-env.sh", "env_name": "dev", "config_files": FILES, "allowed_project_ids": ["my-app-dev"],
           "config_source": str(src)}
    with pytest.raises(fr.RealBackendRefused, match="my-app-test"):
        fr.swap_env(cfg, co, log_file=tmp_path / "log")
    assert (co / "restored.txt").exists()                      # refusal restored the placeholders
    # now with the dev configs staged from the machine-local source
    _plist(src / "GoogleService-Info.plist", "my-app-dev"); _gsjson(src / "google-services.json", "my-app-dev")
    info = fr.swap_env(cfg, co, log_file=tmp_path / "log")
    assert info["project_id"] == "my-app-dev"
    assert (co / "firebase/config/dev/google-services.json").exists()
    fr.scrub_real_configs(cfg, co)
    assert not (co / "firebase/config/dev").exists()


def test_credentials_must_be_a_service_account_of_the_allowed_project(tmp_path):
    p = tmp_path / "sa.json"
    p.write_text(json.dumps({"type": "service_account", "client_email": "bot@my-app-dev.iam.gserviceaccount.com", "private_key": "x"}))
    assert "project_id=None" in fr.check_credentials(str(p), "my-app-dev")[1]      # gcloud-derived copies lack it; the SDK needs it
    p.write_text(json.dumps({"type": "service_account", "project_id": "my-app-dev", "client_email": "bot@my-app-dev.iam.gserviceaccount.com", "private_key": "x"}))
    assert fr.check_credentials(str(p), "my-app-dev")[0] == p
    assert "does not belong" in fr.check_credentials(str(p), "my-app-test")[1]
    user = tmp_path / "adc.json"
    user.write_text(json.dumps({"type": "authorized_user", "refresh_token": "r"}))
    assert "personal user credentials are refused" in fr.check_credentials(str(user), "my-app-dev")[1]
    assert "does not exist" in fr.check_credentials(str(tmp_path / "missing.json"), "my-app-dev")[1]
    assert "not set" in fr.check_credentials(None, "my-app-dev")[1]


def _cleanup_cfg(tmp_path: Path, sweep_exit=0, matched_after=0, print_result=True, verify_exit=0) -> dict:
    sa = tmp_path / "sa.json"
    sa.write_text(json.dumps({"type": "service_account", "project_id": "my-app-dev", "client_email": "bot@my-app-dev.iam.gserviceaccount.com"}))
    result = 'echo "SWEEP_RESULT {\\"users\\": 2, \\"trees\\": 3, \\"left\\": 0}"' if print_result else "true"
    return {"cleanup": {
        "credentials": str(sa),
        "command": f'test "$GOOGLE_APPLICATION_CREDENTIALS" = "{sa}" && echo prefix={{account_prefix}} project={{project_id}} && {result} && exit {sweep_exit}',
        "verify": f'echo "SWEEP_RESULT {{\\"matched\\": {matched_after}}}" && exit {verify_exit}',
    }}


def test_cleanup_success_verified_and_marker_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    co = tmp_path / "co"; co.mkdir()
    fr.mark_needs_cleanup("run-1", {"project_id": "my-app-dev", "account_prefix": "cac-run-1-", "error": "earlier"})
    res = fr.run_cleanup(_cleanup_cfg(tmp_path), checkout=co, values={}, run_id="run-1", project_id="my-app-dev", storage_bucket="b",
                         log_file=tmp_path / "cleanup.log")
    assert res["ok"] and res["deleted_users"] == 2 and res["deleted_trees"] == 3 and res["remaining"] == 0 and res["verified"]
    assert fr.list_needs_cleanup() == []
    log = (tmp_path / "cleanup.log").read_text()
    assert "prefix=cac-run-1- project=my-app-dev" in log


@pytest.mark.parametrize("kw,needle", [
    (dict(sweep_exit=1), "exited 1"),
    (dict(matched_after=2), "2 account(s) with prefix cac-run-2- still exist"),
    (dict(matched_after=1, verify_exit=1), "1 account(s) with prefix cac-run-2- still exist"),   # a strict dry run exits 1 on matches
    (dict(print_result=False), "no SWEEP_RESULT"),
])
def test_cleanup_failure_is_flagged_loudly(tmp_path, monkeypatch, kw, needle):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    co = tmp_path / "co"; co.mkdir()
    res = fr.run_cleanup(_cleanup_cfg(tmp_path, **kw), checkout=co, values={}, run_id="run-2", project_id="my-app-dev", storage_bucket=None,
                         log_file=tmp_path / "cleanup.log")
    assert not res["ok"] and res["needs_cleanup"] and needle in res["error"]
    marks = fr.list_needs_cleanup()
    assert marks and marks[0]["run_id"] == "run-2" and marks[0]["retry"] == "codeandconfirm cleanup run-2"


def test_cleanup_refuses_wrong_project_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    cfg = _cleanup_cfg(tmp_path)
    res = fr.run_cleanup(cfg, checkout=tmp_path, values={}, run_id="run-3", project_id="my-app-test", storage_bucket=None, log_file=tmp_path / "l")
    assert not res["ok"] and "does not belong to project my-app-test" in res["error"]
    assert "exit_code" not in res                       # the sweep was never executed


def test_gate_and_report_flag_missing_cleanup(tmp_path):
    from test_gate import make_cfg, good_ctx, CAND
    cfg = make_cfg(tmp_path)
    ctx = good_ctx(tmp_path, backend={"kind": "firebase-real", "detail": {"project_id": "my-app-dev", "env_name": "dev", "account_prefix": "cac-r-"}},
                   backend_cleanup=None)
    g = evaluate(cfg, CAND, ctx)
    assert g.verdict == "PASS"                                  # housekeeping never changes the product verdict …
    chk = next(c for c in g.checks if c.name == "backend.cleanup")
    assert not chk.ok and not chk.required and "needs cleanup" in chk.detail
    md = render_markdown({"run_id": "r", "created_at": "t"}, CAND, cfg.data, ctx, g, tmp_path)
    assert "NEEDS CLEANUP" in md.splitlines()[2]                # … but the report shouts it at the top
    assert "backend: real project my-app-dev" in md
    ctx["backend_cleanup"] = {"ok": True, "deleted_users": 1, "deleted_trees": 1, "verified": True, "remaining": 0}
    g2 = evaluate(cfg, CAND, ctx)
    assert next(c for c in g2.checks if c.name == "backend.cleanup").ok
    assert "NEEDS CLEANUP" not in render_markdown({"run_id": "r", "created_at": "t"}, CAND, cfg.data, ctx, g2, tmp_path)


def test_parse_sweep_result_takes_last_line():
    out = "noise\nSWEEP_RESULT {\"users\": 1}\nmore\nSWEEP_RESULT {\"users\": 4, \"matched\": 0}\n"
    assert fr._parse_sweep_result(out) == {"users": 4, "matched": 0}
    assert fr._parse_sweep_result("nothing") is None
    assert fr._parse_sweep_result("SWEEP_RESULT not-json") is None
