# Configuration reference

Three layers, lowest to highest precedence:

1. `$CODEANDCONFIRM_HOME/config.toml` — machine-wide (`~/.codeandconfirm/config.toml` by default)
2. `<repo>/codeandconfirm.toml` — committed project config; placeholders only
3. `<repo>/.codeandconfirm.local.toml` — gitignored machine overrides

`codeandconfirm config` prints the effective merge. A run snapshots its configuration at creation
(`runs/<id>/config.snapshot.json`); `resume` always uses the snapshot.

## `[project]`
| key | default | meaning |
|---|---|---|
| `name` | repo dir name | shown in reports and prompts |
| `default_base` | `main` | base ref when `--base` is omitted |

## `[profiles.<name>]` and `--profile`
A profile is a partial config merged over the layered result. `[project].default_profile` names the one used
when `--profile` is omitted (`pr` is used when no default is set and a `pr` profile exists). The shipped example
defines:

| profile | when | what it changes |
|---|---|---|
| `pr` (default) | every change | `codex.reasoning_effort = medium`, `qa.required_journeys = ["auth.signup"]`, `qa.require_diff_scenarios = true`, `qa.base_comparison = false`, worker timeout 15 min (about ten minutes of QA on top of builds and suites) |
| `full` | weekly | effort `high`, every journey, every suite, `perf.enabled = true`, longer timeouts; the family-tree example also switches `backend.kind` to `firebase-real` |

Any table can be overridden per profile, and a machine-local file may override a profile's values through its own
`[profiles.<name>.*]` table. The run records its profile (`status`, report header, approval record); approvals
are per configuration fingerprint, so a `pr` PASS never satisfies a `gate-check --profile full`.
`examples/weekly/` has a launchd agent, a GitHub Actions workflow and the driver script for the weekly run.

## `[codex]`
| key | default | meaning |
|---|---|---|
| `binary` | auto | Codex CLI path; auto = `codex` on PATH, then known app bundles. `doctor` shows which binary serves the model |
| `model` | `gpt-6-astra` | exact model slug; verified against `codex debug models` and against the session rollout after each worker. Never substituted |
| `reasoning_effort` | `high` | one of the model's supported efforts |
| `sandbox` | `danger-full-access` | worker sandbox for trusted candidates; forks always get `read-only` |
| `extra_args` | `[]` | appended to `codex exec` |

## `[roles]` and `[claude]`
| key | default | meaning |
|---|---|---|
| `roles.lead` | `claude` | who implements (`claude` · `codex`); informational, and `doctor` warns when lead and QA are the same vendor |
| `roles.qa` | `codex` | who reviews and tests: `codex` runs `codex exec`; `claude` runs `claude -p` with the **same** role prompt, task and verdict schema |
| `claude.binary` | auto (`claude` on PATH) | Claude Code CLI |
| `claude.model` | `claude-opus-5` | exact model id; verified per worker from the session transcript's assistant messages (a dated variant of the same id is accepted), never from the model's own claim |
| `claude.reasoning_effort` | inherits `codex.reasoning_effort` | `low` · `medium` · `high` · `xhigh` · `max` (`--effort`) |
| `claude.extra_args` | `[]` | appended to `claude -p` |

With `qa = "claude"` trusted candidates run with `--dangerously-skip-permissions` (device tools need it), forks get
`--tools Read,Grep,Glob`; the worker loads only project-level settings (`--setting-sources project`), so your global
hooks and instruction files stay out of the QA session. Runs, reports and `status` record the engine.
To make **Codex the lead**, point its instructions at `skills/codeandconfirm/for-codex-lead.md` and set
`roles = { lead = "codex", qa = "claude" }`.

## `[qa]`
| key | default | meaning |
|---|---|---|
| `platforms` | `["ios","android"]` | platforms to build, install and QA |
| `parallel_platforms` | `true` | run platform workers (and builds/suites) concurrently |
| `static_review` | `true` | also run the device-free review worker |
| `required_suites` | `[]` | suite names the coordinator must run green |
| `required_journeys` | `[]` | journey ids every platform worker must report `passed` with evidence |
| `min_interactions_per_platform` | `6` | taps/types/keys/scrolls the worker itself must perform |
| `block_severity` | `high` | findings at/above this fail the run (`critical`,`high`,`medium`,`low`) |
| `max_repair_cycles` | `5` | FAIL→fix cycles per branch before the run refuses (durable counter) |
| `timeout_minutes` | `90` | whole run budget (advisory in reports) |
| `worker_timeout_minutes` | `60` | per Codex worker; an unfinished worker is BLOCKED |
| `infra_retries` | `1` | retries for suites/workers that produced no result at all |
| `base_comparison` | `true` | also build the base SHA so device workers can attribute a defect (`pr` profile turns it off) |
| `require_diff_scenarios` | `false` | a platform whose product code the diff touches must report at least one `diff.<slug>` scenario, else BLOCKED |

## `[backend]`
| key | meaning |
|---|---|
| `kind` | `none` · `command` · `firebase-emulator` · `firebase-real` |
| `workdir`, `prepare`, `start`, `env`, `health` | how to start it and know it is up |
| `ports` | named base ports; rendered as `{<name>_port}` |
| `port_isolation` | `none` · `per-run` · `ios-only` (see backend.py docstring) |

`firebase-real` (development project only — see [real-backend.md](real-backend.md)):

| key | meaning |
|---|---|
| `env_script`, `env_name`, `restore_arg` | the repo's config-swap script, run as `<script> <env_name>`; `restore_arg` restores placeholders (default `emulator`) |
| `allowed_project_ids` | project ids the swapped configs may name; anything else → BLOCKED. Never list test or prod |
| `config_files` | swapped tracked config paths; the project id is read from them, and they are whitelisted in the pristine check |
| `config_source` (machine-local) | directory with the real client configs, copied into `config_dest` (default `firebase/config/<env_name>`) |
| `cleanup.command`, `cleanup.verify` | delete / re-count every `{account_prefix}` account; stdout ends with `SWEEP_RESULT {...}` |
| `cleanup.credentials` (machine-local) | service-account key **of the allowed project** (with `project_id`), outside the repo; exported as `GOOGLE_APPLICATION_CREDENTIALS` |

## `[platforms.<ios|android>]`
`build`, `env`, artifact path (`app` / `apk`), `bundle_id` + `launch_args` (iOS), `package` + `activity` (Android),
`device_name` + `device_type` (iOS simulator to create), `avd_name` + `port` (Android emulator to boot).

## `[suites.<name>]`
`platform` (`ios`, `android`, or `backend`), `command`, `env`, `results = { kind, path }` with kind
`xcresult` · `junit` · `junit-dir` · `none`.

## `[journeys]` / `[journeys.catalog]`
`required` list and a catalog `id = "plain-language description"`. Ids are what workers report.

## `[perf]`
`enabled` (default false), `samples`, `journeys` (`startup`), `budgets = { startup_ms_percent = 25 }`,
`required` (default false: inconclusive measurements are reported, not blocking),
`ios_ready_log_marker` (a log line your iOS app prints once ready; enables millisecond startup timing from
the unified log instead of accessibility polling).

## `[github]`
`publish` (default false), `status_context` (`codeandconfirm/qa`).

## `[trust]`
`external_prs = static-only | refuse`.

## `[tools]` (machine-local)
`jdk17_home`, `jdk21_home`, `android_sdk` — auto-detected from Homebrew / `ANDROID_HOME` when empty.

## `[scheduler]` (machine-local)
| key | default | meaning |
|---|---|---|
| `max_functional_runs` | `0` (auto: 1 per 24 GB RAM) | concurrent device runs on this host |
| `min_free_memory_percent` | `15` | refuse to start below this |
| `max_load_per_core` | `0.85` | refuse to start above load × cores |
| `ci_worker_process_pattern` | `Runner.Worker` | how to detect busy CI jobs on the same host (benchmarks refuse when > 0) |
| `wait_for_capacity_minutes` | `20` | how long to wait before BLOCKED |
| `lock_ttl_minutes` | `180` | reservation TTL; renewed by heartbeat while a run is alive |

## `[artifacts]`
`retention_days` (used by `gc`), `keep_screenshots`, `redact`.

## Placeholders

`{run_id} {run_dir} {checkout_dir} {artifacts_dir} {evidence_dir} {cache_dir} {candidate_sha} {base_sha}
{ios_udid} {android_serial} {jdk17_home} {jdk21_home} {android_sdk}` and `{<port-name>_port}` for each backend port;
`{project_id} {account_prefix} {storage_bucket}` (real backend) and `{credentials}` (cleanup commands).
Unknown placeholders are an error in commands and left literal in `env` values.
