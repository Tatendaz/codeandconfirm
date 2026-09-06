"""Real Firebase backend (development project only): swap the apps' client configs to the named
environment, verify the active project id against an allow-list, and reclaim every account the
run created once QA is over.

Three environments, one rule:
  dev   CodeAndConfirm may create and delete anything under its own account prefix
  test  humans only (release rehearsals); CodeAndConfirm refuses it
  prod  never

`[backend]` keys for `kind = "firebase-real"`:
  env_script            repo-relative script that swaps the client configs; run as `<script> <env_name>`
  env_name              environment argument for the script (default "dev")
  restore_arg           argument that restores the committed placeholder configs (default "emulator")
  allowed_project_ids   project ids the swapped configs may name; anything else is refused (BLOCKED)
  config_files          the swapped, tracked config paths; the project id is read from them and they are
                        whitelisted in the pristine check
  config_source         MACHINE-LOCAL (never committed): directory holding the real configs; copied into
                        `config_dest` before the swap because the repo gitignores them
  config_dest           where the script expects them (default "firebase/config/{env_name}")
  health                URLs that must answer (default: Firestore + Identity Toolkit endpoints)
  prepare               commands to run first (e.g. `npm install` for the cleanup tooling)

`[backend.cleanup]`:
  command       deletes every account whose email starts with `{account_prefix}` and all its data; its stdout
                must end with a `SWEEP_RESULT {...json...}` line (keys: users, trees, left, matched)
  verify        re-lists the remaining accounts without deleting (same SWEEP_RESULT line); `matched` must be 0
  credentials   MACHINE-LOCAL path (outside the repo) to a service-account key for the allowed project only;
                exported as GOOGLE_APPLICATION_CREDENTIALS. Keys of any other project are refused.

A cleanup that fails, cannot run, or leaves accounts behind writes a durable marker under
$CODEANDCONFIRM_HOME/needs-cleanup/<run-id>.json, is flagged at the top of the report and in
`codeandconfirm status`, and can be retried with `codeandconfirm cleanup <run-id>`.
"""

from __future__ import annotations

import json
import plistlib
import shutil
from pathlib import Path

from .config import home_dir, render, render_env
from .util import expand, now_iso, read_json, run, write_json

DEFAULT_HEALTH = ["https://firestore.googleapis.com/", "https://identitytoolkit.googleapis.com/"]
DEFAULT_CLEANUP_TIMEOUT_S = 15 * 60


class RealBackendRefused(RuntimeError):
    """The swapped configuration names a project CodeAndConfirm may not touch."""


def account_prefix(run_id: str) -> str:
    """Every account a run creates starts with this; the sweep deletes exactly this prefix."""
    return f"cac-{run_id}-"


# --- config files -------------------------------------------------------------------------


def read_client_config(path: Path) -> dict:
    """Project id (and storage bucket) named by an iOS GoogleService-Info.plist or an Android
    google-services.json. Unknown formats return an empty dict."""
    if not path.exists():
        return {}
    if path.suffix == ".plist":
        try:
            with open(path, "rb") as fh:
                d = plistlib.load(fh)
        except Exception:  # noqa: BLE001 — unreadable config is reported as "no project id"
            return {}
        return {"project_id": d.get("PROJECT_ID"), "storage_bucket": d.get("STORAGE_BUCKET"), "path": str(path)}
    if path.suffix == ".json":
        d = read_json(path, {}) or {}
        info = d.get("project_info") or {}
        return {"project_id": info.get("project_id") or d.get("project_id"), "storage_bucket": info.get("storage_bucket"), "path": str(path)}
    return {}


def verify_project(checkout: Path, config_files: list[str], allowed: list[str]) -> dict:
    """Read every swapped config; all must name the same project and it must be allow-listed.

    Returns {"project_id", "storage_bucket", "configs": [...]} or raises RealBackendRefused."""
    allowed = [a for a in (allowed or []) if a]
    if not allowed:
        raise RealBackendRefused("backend.allowed_project_ids is empty; a real backend needs an explicit allow-list (dev project only)")
    if not config_files:
        raise RealBackendRefused("backend.config_files is empty; list the swapped client config paths so the project id can be verified")
    infos = [read_client_config(checkout / f) for f in config_files]
    ids = {i.get("project_id") for i in infos}
    if None in ids or not ids:
        missing = [f for f, i in zip(config_files, infos) if not i.get("project_id")]
        raise RealBackendRefused(f"could not read a project id from {missing}; refusing to run against an unknown project")
    if len(ids) > 1:
        raise RealBackendRefused(f"swapped configs disagree on the project: {sorted(ids)}; refusing")
    pid = ids.pop()
    if pid not in allowed:
        raise RealBackendRefused(f"project {pid!r} is not in backend.allowed_project_ids {allowed}; CodeAndConfirm only runs against the development project")
    bucket = next((i.get("storage_bucket") for i in infos if i.get("storage_bucket")), None)
    return {"project_id": pid, "storage_bucket": bucket, "configs": infos}


# --- swap / restore --------------------------------------------------------------------------


def _env_bits(cfg_backend: dict) -> tuple[str, str, str, str]:
    env_name = str(cfg_backend.get("env_name", "dev"))
    script = str(cfg_backend.get("env_script", "scripts/use-firebase-env.sh"))
    restore = str(cfg_backend.get("restore_arg", "emulator"))
    dest = str(cfg_backend.get("config_dest") or f"firebase/config/{env_name}")
    return env_name, script, restore, dest


def stage_real_configs(cfg_backend: dict, checkout: Path) -> Path | None:
    """Copy the machine-local real client configs into the checkout where the swap script expects them.
    The repo gitignores that directory, so a fresh worktree never has it."""
    env_name, _, _, dest_rel = _env_bits(cfg_backend)
    dest = checkout / dest_rel
    src_cfg = cfg_backend.get("config_source")
    if src_cfg:
        src = expand(src_cfg)
        if not src.is_dir():
            raise RealBackendRefused(f"backend.config_source {src} is not a directory (set it in .codeandconfirm.local.toml)")
        dest.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file():
                shutil.copy2(f, dest / f.name)
        return dest
    if dest.is_dir() and any(dest.iterdir()):
        return dest
    raise RealBackendRefused(f"no real client configs for environment {env_name!r}: {dest} is empty and backend.config_source is not set")


def swap_env(cfg_backend: dict, checkout: Path, *, log_file: Path, record_to: Path | None = None) -> dict:
    """Stage configs, run the repo's swap script for `env_name`, and verify the active project id.
    Raises RealBackendRefused (never leaves a refused config in place)."""
    env_name, script, restore, _ = _env_bits(cfg_backend)
    stage_real_configs(cfg_backend, checkout)
    if not (checkout / script).exists():
        raise RealBackendRefused(f"backend.env_script {script} not found in the checkout")
    r = run(["/bin/bash", script, env_name], cwd=checkout, timeout=120, log_file=log_file, record_to=record_to, label="backend-env-swap")
    if not r.ok:
        raise RealBackendRefused(f"{script} {env_name} failed (exit {r.exit_code}): {r.stderr.strip()[-300:]}")
    try:
        return verify_project(checkout, list(cfg_backend.get("config_files") or []), list(cfg_backend.get("allowed_project_ids") or []))
    except RealBackendRefused:
        run(["/bin/bash", script, restore], cwd=checkout, timeout=120, log_file=log_file, label="backend-env-restore")
        raise


def scrub_real_configs(cfg_backend: dict, checkout: Path, *, log_file: Path | None = None) -> None:
    """Restore the committed placeholder configs and delete the staged real ones (best effort)."""
    _, script, restore, dest_rel = _env_bits(cfg_backend)
    if not checkout.exists():
        return
    if (checkout / script).exists():
        run(["/bin/bash", script, restore], cwd=checkout, timeout=120, log_file=log_file, label="backend-env-restore")
    if cfg_backend.get("config_source"):
        shutil.rmtree(checkout / dest_rel, ignore_errors=True)


def health_ok(urls: list[str], timeout: float = 5.0) -> list[str]:
    """URLs that did not answer with any HTTP status (network down / blocked)."""
    import urllib.error
    import urllib.request
    bad = []
    for u in urls:
        try:
            with urllib.request.urlopen(u, timeout=timeout):  # noqa: S310 — configured health URL
                pass
        except urllib.error.HTTPError:
            continue        # any HTTP answer means the endpoint is reachable
        except Exception:  # noqa: BLE001
            bad.append(u)
    return bad


# --- cleanup ------------------------------------------------------------------------------


def _parse_sweep_result(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("SWEEP_RESULT "):
            try:
                d = json.loads(line[len("SWEEP_RESULT "):])
                return d if isinstance(d, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def check_credentials(path_cfg: str | None, project_id: str) -> tuple[Path | None, str | None]:
    """The cleanup key must be a service account of the allowed project itself: a key for any other
    project (or a personal user credential) is refused so the sweep can never reach test or prod."""
    if not path_cfg:
        return None, "backend.cleanup.credentials is not set (machine-local path to the dev service-account key)"
    p = expand(path_cfg)
    if not p.exists():
        return None, f"credentials file {p} does not exist"
    d = read_json(p, None)
    if not isinstance(d, dict):
        return None, f"credentials file {p} is not JSON"
    if d.get("type") != "service_account":
        return None, f"credentials file {p} is not a service-account key (type={d.get('type')!r}); personal user credentials are refused"
    email = str(d.get("client_email") or "")
    if not email.endswith(f"@{project_id}.iam.gserviceaccount.com"):
        return None, f"service account {email or '?'} does not belong to project {project_id}; refusing to use it"
    if d.get("project_id") != project_id:
        # the Admin SDK needs project_id in the key; a gcloud-stored copy (legacy_credentials/*/adc.json) lacks it
        return None, (f"credentials file {p} has project_id={d.get('project_id')!r}, expected {project_id!r} "
                      f"(a service-account key downloaded from the console has it; add it if you derived the file from gcloud)")
    return p, None


def run_cleanup(cfg_backend: dict, *, checkout: Path, values: dict, run_id: str, project_id: str,
                storage_bucket: str | None, log_file: Path, record_to: Path | None = None,
                timeout_s: float = DEFAULT_CLEANUP_TIMEOUT_S, log=lambda s: None) -> dict:
    """Delete every `cac-<run-id>-` account and its data from the real project, then verify none remain.

    Never raises: the result is recorded and flagged; a failure is loud, not fatal."""
    cl = dict(cfg_backend.get("cleanup") or {})
    prefix = account_prefix(run_id)
    res: dict = {"ok": False, "project_id": project_id, "account_prefix": prefix, "started_at": now_iso(), "log_file": str(log_file),
                 "deleted_users": None, "deleted_trees": None, "remaining": None, "verified": False, "error": None}
    if not cl.get("command"):
        res["error"] = "backend.cleanup.command is not configured"
        return _finish_cleanup(res, run_id, log)
    creds, why = check_credentials(cl.get("credentials"), project_id)
    if creds is None:
        res["error"] = why
        return _finish_cleanup(res, run_id, log)
    if not checkout.exists():
        res["error"] = f"checkout {checkout} no longer exists; run the sweep by hand"
        return _finish_cleanup(res, run_id, log)
    vals = dict(values, project_id=project_id, account_prefix=prefix, credentials=str(creds), storage_bucket=storage_bucket or "")
    env = render_env(cl.get("env"), vals)
    env["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds)
    try:
        cmd = render(str(cl["command"]), vals)
    except KeyError as e:
        res["error"] = f"cleanup command has an unknown placeholder: {e}"
        return _finish_cleanup(res, run_id, log)
    log(f"backend cleanup: deleting accounts {prefix}* from real project {project_id}")
    r = run(cmd, cwd=checkout, env=env, timeout=timeout_s, log_file=log_file, record_to=record_to, label="backend-cleanup")
    res["exit_code"] = r.exit_code
    sweep = _parse_sweep_result(r.stdout)
    if sweep:
        res["deleted_users"] = sweep.get("users"); res["deleted_trees"] = sweep.get("trees"); res["left"] = sweep.get("left")
    if not r.ok:
        res["error"] = f"cleanup command exited {r.exit_code}{' (timeout)' if r.timed_out else ''}; see {log_file}"
    elif sweep is None:
        res["error"] = "cleanup command printed no SWEEP_RESULT line; cannot confirm what was deleted"
    if cl.get("verify"):
        try:
            vcmd = render(str(cl["verify"]), vals)
            v = run(vcmd, cwd=checkout, env=env, timeout=timeout_s, log_file=log_file, record_to=record_to, label="backend-cleanup-verify")
            vs = _parse_sweep_result(v.stdout)
            if v.ok and vs is not None and "matched" in vs:
                res["remaining"] = int(vs.get("matched") or 0)
                res["verified"] = True
                if res["remaining"] > 0 and not res["error"]:
                    res["error"] = f"{res['remaining']} account(s) with prefix {prefix} still exist after the sweep"
            elif not res["error"]:
                res["error"] = f"verification exited {v.exit_code} without a SWEEP_RESULT line"
        except KeyError as e:
            res["error"] = res["error"] or f"verify command has an unknown placeholder: {e}"
    res["ok"] = res["error"] is None and (not cl.get("verify") or res["remaining"] == 0)
    return _finish_cleanup(res, run_id, log)


def _finish_cleanup(res: dict, run_id: str, log) -> dict:
    res["finished_at"] = now_iso()
    res["needs_cleanup"] = not res["ok"]
    if res["ok"]:
        clear_needs_cleanup(run_id)
        log(f"backend cleanup ok: {res.get('deleted_users')} account(s), {res.get('deleted_trees')} tree(s) removed; remaining {res.get('remaining')}")
    else:
        mark_needs_cleanup(run_id, res)
        log(f"NEEDS CLEANUP: real project {res['project_id']} may still hold accounts {res['account_prefix']}* — {res['error']} "
            f"(retry: codeandconfirm cleanup {run_id})")
    return res


# --- durable "needs cleanup" markers ------------------------------------------------------------


def needs_cleanup_dir() -> Path:
    d = home_dir() / "needs-cleanup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mark_needs_cleanup(run_id: str, res: dict) -> Path:
    p = needs_cleanup_dir() / f"{run_id}.json"
    write_json(p, {"run_id": run_id, "project_id": res.get("project_id"), "account_prefix": res.get("account_prefix"),
                   "error": res.get("error"), "marked_at": now_iso(), "retry": f"codeandconfirm cleanup {run_id}"})
    return p


def clear_needs_cleanup(run_id: str) -> None:
    (needs_cleanup_dir() / f"{run_id}.json").unlink(missing_ok=True)


def list_needs_cleanup() -> list[dict]:
    return [d for d in (read_json(p, None) for p in sorted(needs_cleanup_dir().glob("*.json"))) if isinstance(d, dict)]
