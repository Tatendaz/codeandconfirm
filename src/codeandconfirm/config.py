"""Configuration: layered TOML (user → repo → local override) plus placeholder rendering.

Files, lowest to highest precedence:
  1. $CODEANDCONFIRM_HOME/config.toml          machine-wide (codex binary, tool paths, scheduler)
  2. <repo>/codeandconfirm.toml                 committed project config (placeholders only)
  3. <repo>/.codeandconfirm.local.toml          gitignored machine-specific overrides

Nothing personal belongs in (2). Device names, ports and JDK paths belong in (1) or (3),
or are auto-detected by `doctor`.

Profiles: `[profiles.<name>]` tables are partial configs merged over the result when that profile
is selected (`--profile <name>`, else `[project].default_profile`, else `pr` when defined). A profile
can override any table — `qa`, `codex`, `perf`, `backend`, … — so one committed file describes both
the per-PR run and the weekly full run.
"""

from __future__ import annotations

import os
import re
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .util import expand

HOME_ENV = "CODEANDCONFIRM_HOME"
REPO_CONFIG_NAME = "codeandconfirm.toml"
LOCAL_CONFIG_NAME = ".codeandconfirm.local.toml"

DEFAULTS: dict[str, Any] = {
    "project": {"name": "", "default_base": "main", "default_profile": ""},
    "profiles": {},
    "profile": {"name": "", "description": ""},        # filled in by Config.load when a profile is applied
    "codex": {
        "binary": "",                 # empty → resolved by doctor (PATH, then known app bundles)
        "model": "gpt-6-astra",
        "reasoning_effort": "high",
        "sandbox": "danger-full-access",  # required for simctl/adb/idb; see docs/trust-boundaries.md
        "extra_args": [],
    },
    "qa": {
        "platforms": ["ios", "android"],
        "parallel_platforms": True,
        "static_review": True,
        "required_suites": [],
        "required_journeys": [],
        "min_interactions_per_platform": 6,
        "block_severity": "high",     # critical|high|medium|low — findings at/above fail the gate
        "max_repair_cycles": 5,
        "timeout_minutes": 90,
        "worker_timeout_minutes": 60,
        "infra_retries": 1,
        "base_comparison": True,          # also build the base SHA so device workers can attribute defects
        "require_diff_scenarios": False,  # gate: a platform the diff touches must report >= 1 diff.* scenario
    },
    "backend": {"kind": "none"},
    # Who builds and who tests. Default: Claude Code implements, a Codex worker reviews/tests. `qa = "claude"`
    # runs the same role prompt and verdict schema through `claude -p`, with the model verified from the
    # session transcript. Same vendor on both sides still works, but doctor warns: independence is the point.
    "roles": {"lead": "claude", "qa": "codex"},
    "claude": {"binary": "", "model": "claude-opus-5", "reasoning_effort": "", "extra_args": []},   # effort "" → inherit codex.reasoning_effort
    "platforms": {},
    "suites": {},
    "journeys": {"required": [], "catalog": {}},
    "perf": {"enabled": False, "samples": 5, "budgets": {}, "journeys": ["startup"]},
    "github": {"publish": False, "status_context": "codeandconfirm/qa"},
    "trust": {"external_prs": "static-only"},   # refuse | static-only
    "tools": {"jdk17_home": "", "jdk21_home": "", "android_sdk": ""},
    "scheduler": {
        "max_functional_runs": 0,             # 0 → auto from RAM (1 per 24 GB, min 1)
        "min_free_memory_percent": 15,
        "max_load_per_core": 0.85,
        "ci_worker_process_pattern": "Runner.Worker",
        "wait_for_capacity_minutes": 20,
        "lock_ttl_minutes": 180,
    },
    "artifacts": {"retention_days": 14, "keep_screenshots": True, "redact": True},
}


def home_dir() -> Path:
    return expand(os.environ.get(HOME_ENV) or "~/.codeandconfirm")


def _deep_merge(base: dict, over: Mapping) -> dict:
    out = deepcopy(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def find_repo_root(start: Path | None = None) -> Path | None:
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


class Config:
    def __init__(self, data: dict, sources: list[Path], repo_root: Path | None):
        self.data = data
        self.sources = sources
        self.repo_root = repo_root

    @classmethod
    def load(cls, repo_root: Path | None = None, explicit: Path | None = None, profile: str | None = None) -> "Config":
        sources: list[Path] = []
        data = deepcopy(DEFAULTS)
        user_cfg = home_dir() / "config.toml"
        if user_cfg.exists():
            data = _deep_merge(data, _load_toml(user_cfg)); sources.append(user_cfg)
        if explicit:
            data = _deep_merge(data, _load_toml(explicit)); sources.append(explicit)
            repo_root = repo_root or find_repo_root(explicit.parent)
        elif repo_root:
            rc = repo_root / REPO_CONFIG_NAME
            if rc.exists():
                data = _deep_merge(data, _load_toml(rc)); sources.append(rc)
        if repo_root:
            lc = repo_root / LOCAL_CONFIG_NAME
            if lc.exists():
                data = _deep_merge(data, _load_toml(lc)); sources.append(lc)
        data = apply_profile(data, profile)
        return cls(data, sources, repo_root)

    # convenience accessors ---------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, Mapping) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def platforms(self) -> dict[str, dict]:
        return {k: v for k, v in self.data.get("platforms", {}).items() if k in self.get("qa.platforms", [])}

    @property
    def suites(self) -> dict[str, dict]:
        return dict(self.data.get("suites", {}))


def apply_profile(data: dict, profile: str | None) -> dict:
    """Merge `[profiles.<name>]` over the layered config. Explicit name → must exist. No name → the project's
    `default_profile`, else `pr` when such a profile is defined, else nothing happens."""
    profiles = data.get("profiles") or {}
    name = profile or (data.get("project") or {}).get("default_profile") or ("pr" if "pr" in profiles else "")
    if not name:
        return data
    if name not in profiles:
        raise ValueError(f"unknown profile {name!r}; defined profiles: {sorted(profiles) or 'none'}")
    over = {k: v for k, v in profiles[name].items() if k != "description"}
    out = _deep_merge(data, over)
    out["profile"] = {"name": name, "description": str(profiles[name].get("description", ""))}
    return out


_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


def render(template: str, values: Mapping[str, Any], *, strict: bool = True) -> str:
    """Substitute `{name}` placeholders. Unknown names raise (strict) or stay literal."""

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in values and values[key] is not None:
            return str(values[key])
        if strict:
            raise KeyError(f"unknown placeholder {{{key}}} in: {template}")
        return m.group(0)

    return _PLACEHOLDER.sub(sub, template)


def render_env(env: Mapping[str, str] | None, values: Mapping[str, Any]) -> dict[str, str]:
    return {k: render(str(v), values, strict=False) for k, v in (env or {}).items()}


EXAMPLE_CONFIG = """\
# codeandconfirm.toml — project configuration (safe to commit: placeholders only).
# Machine-specific values (device names, JDK paths, codex binary) go in
# .codeandconfirm.local.toml (gitignored) or $CODEANDCONFIRM_HOME/config.toml.
#
# Placeholders available in commands: {run_dir} {checkout_dir} {artifacts_dir} {evidence_dir}
# {cache_dir} {run_id} {candidate_sha} {base_sha} {ios_udid} {android_serial}
# {auth_port} {firestore_port} {storage_port} {functions_port} {jdk17_home} {jdk21_home} {android_sdk}

[project]
name = "my-mobile-app"
default_base = "main"
default_profile = "pr"        # `review --profile pr|full`; pr is the per-change default, full the weekly run

[codex]
model = "gpt-6-astra"         # doctor verifies this exact model is available; never substituted
reasoning_effort = "high"

# Profiles are partial configs merged over everything above when selected.
[profiles.pr]
description = "Per change: sign-up + scenarios derived from the diff, medium effort, ~10 minutes"
[profiles.pr.codex]
reasoning_effort = "medium"
[profiles.pr.qa]
required_journeys = ["auth.signup"]
require_diff_scenarios = true      # a platform the diff touches must report >= 1 diff.<slug> scenario
base_comparison = false            # skip the base build; attribution happens in the weekly full run
worker_timeout_minutes = 15
timeout_minutes = 40

[profiles.full]
description = "Weekly: every journey, every suite, benchmark, high effort"
[profiles.full.codex]
reasoning_effort = "high"
[profiles.full.qa]
required_journeys = ["auth.signup", "tree.create-once", "person.add", "nav.back", "app.restart"]
worker_timeout_minutes = 90
timeout_minutes = 240
[profiles.full.perf]
enabled = true

[qa]
platforms = ["ios", "android"]
parallel_platforms = true
required_suites = ["ios-ui", "android-ui"]
required_journeys = ["auth.signup", "tree.create-once", "person.add", "nav.back", "app.restart"]
min_interactions_per_platform = 6
block_severity = "high"
max_repair_cycles = 5
timeout_minutes = 90

[backend]
kind = "firebase-emulator"
workdir = "firebase"
prepare = ["cd firebase/functions && npm install --no-audit --no-fund"]
start = "firebase emulators:start --project demo-project --only auth,firestore,storage,functions"
env = { JAVA_HOME = "{jdk21_home}" }
ports = { auth = 9099, firestore = 8080, storage = 9199, functions = 5001 }
health = ["http://127.0.0.1:{auth_port}/", "http://127.0.0.1:{firestore_port}/"]
# "ios-only": iOS build accepts per-run ports; Android hardcodes host ports → serialized by lock.
port_isolation = "ios-only"

[platforms.ios]
device_names = ["CodeAndConfirm iPhone"]     # pool; concurrent runs take the first free one
device_type = "iPhone 17"
build = "xcodebuild build -project ios/App/App.xcodeproj -scheme App -configuration Debug -sdk iphonesimulator -destination 'platform=iOS Simulator,id={ios_udid}' -derivedDataPath {run_dir}/derived-ios -clonedSourcePackagesDirPath {cache_dir}/spm CODE_SIGNING_ALLOWED=NO"
app = "{run_dir}/derived-ios/Build/Products/Debug-iphonesimulator/App.app"
bundle_id = "com.example.app"
launch_args = []        # always passed
reset_args = []         # only for `ccdevice ios launch --reset` (e.g. a sign-out-on-launch test flag)

[platforms.android]
avd_names = ["codeandconfirm_api34"]         # pool, paired with ports below
ports = [5580]
build = "cd android && ./gradlew :app:assembleDebug --no-daemon --console=plain"
env = { JAVA_HOME = "{jdk17_home}" }
apk = "android/app/build/outputs/apk/debug/app-debug.apk"
package = "com.example.app"
activity = ".MainActivity"

[suites.ios-ui]
platform = "ios"
command = "xcodebuild test -project ios/App/App.xcodeproj -scheme App -sdk iphonesimulator -destination 'platform=iOS Simulator,id={ios_udid}' -derivedDataPath {run_dir}/derived-ios -resultBundlePath {artifacts_dir}/ios-ui.xcresult"
results = { kind = "xcresult", path = "{artifacts_dir}/ios-ui.xcresult" }

[suites.android-ui]
platform = "android"
command = "cd android && ./gradlew :app:connectedDebugAndroidTest --no-daemon --console=plain"
env = { JAVA_HOME = "{jdk17_home}", ANDROID_SERIAL = "{android_serial}" }
results = { kind = "junit-dir", path = "android/app/build/outputs/androidTest-results/connected" }

[journeys.catalog]
"auth.signup" = "Sign up with a fresh account, confirm you land on the home list."
"tree.create-once" = "Create one tree; verify exactly one record appears (no duplicates)."
"person.add" = "Add a person with a name and year; verify it appears."
"nav.back" = "Navigate into a detail screen and back; confirm state is intact."
"app.restart" = "Terminate and relaunch; confirm the session and data persist."

[perf]
enabled = false
samples = 5
journeys = ["startup"]
budgets = { startup_ms_percent = 25 }

[github]
publish = false
status_context = "codeandconfirm/qa"

[trust]
external_prs = "static-only"
"""

EXAMPLE_LOCAL_CONFIG = """\
# .codeandconfirm.local.toml — machine-specific overrides (gitignored). Placeholders only here.
[codex]
binary = ""                 # leave empty to let `doctor` resolve; or an absolute path
[tools]
jdk17_home = ""             # e.g. /opt/homebrew/opt/openjdk@17
jdk21_home = ""             # e.g. /opt/homebrew/opt/openjdk
android_sdk = ""            # e.g. ~/Library/Android/sdk
[scheduler]
max_functional_runs = 0     # 0 = auto from RAM
"""
