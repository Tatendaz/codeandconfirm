# Authentication and cost

## Codex

CodeAndConfirm launches `codex exec` with the binary and model you configure (`[codex].binary`, `[codex].model`).
Authentication uses your existing Codex login or API-key setup. CodeAndConfirm's
`doctor` reads `$CODEX_HOME/auth.json` to report login presence, auth mode, and the
last refresh time. It does not copy that file into run artifacts. The Codex CLI
uses its credentials to authenticate with the provider; keep credentials out of
the app repository and test fixtures.

- **Plan login (ChatGPT):** usage counts against your plan's Codex limits. Astra-class models can be
  restricted to certain plans; `codex debug models` shows what your login can use and `doctor` refuses to run
  when the configured model is not listed (it never substitutes another model).
- **API key:** billed per token. Each worker's `events.jsonl` ends with a `turn.completed` record containing
  `input_tokens`, `cached_input_tokens`, `output_tokens` and `reasoning_output_tokens`; the report shows them.

Observed on the first integration target (two-app Firebase project, `high` effort, Astra):

| Worker | Duration | Commands | Input tokens (cached) | Output tokens |
|---|---|---|---|---|
| review (static) | ~8 min | 16 | 0.56 M (most cached) | 6.5 k |
| android (hands-on, 5 journeys, 108 interactions) | ~19 min | 319 | 5.1 M (4.96 M cached) | 15.7 k |
| ios (hands-on) | ~10–20 min | 26–300 | 1.3–5 M (mostly cached) | 6–16 k |

Input tokens are dominated by re-sent context: every step sends the whole session again. The provider counts
cached input at a fraction of the fresh price (about a tenth on OpenAI's published rate card), so a long session
with verbose command output still adds up, and so do three workers where one would do. Ten `high`-effort runs
on the reference project used roughly 40 % of a weekly ChatGPT plan allowance.

## Reducing Codex usage

The `pr` profile written by `codeandconfirm init` applies most of these already; check yours.

| Lever | Where | Effect |
|---|---|---|
| `reasoning_effort = "low"` or `"medium"` | `[profiles.pr.codex]` | fewer reasoning tokens and, in practice, far fewer steps per worker: a `high` Android worker took 319 commands, `medium` workers 30–60 |
| `platforms_from_diff = true` | `[profiles.pr.qa]` | an iOS-only change starts no Android worker and no Android build; shared backend code still tests both; the report names what was skipped |
| `static_review = false` | `[profiles.pr.qa]` | drops the device-free review worker (about 15 % of a run) where another reviewer already reads every PR; keep it in the weekly profile |
| `worker_timeout_minutes` | `[profiles.pr.qa]` or `--worker-timeout` | a worker fills the time it is given; 15 minutes covers one required journey plus the diff scenarios |
| `required_journeys` | `[profiles.pr.qa]` | one required journey per change; the full catalogue belongs to the weekly run |
| `--platforms ios` | command line | one-off narrowing for a single run |

The role prompt also asks the worker to work economically: confirm each step with `wait-for`, `find` or
`tree --grep` instead of re-reading the whole accessibility tree, save screenshots without opening them, filter
logs, and read code in ranges. Those rules live in `src/codeandconfirm/prompts/qa_role.md`; the evidence rules
(screenshots, cited files, real interactions) are unchanged.

## GitHub

`gh` resolves pull requests, and Git fetches their commits. `--publish` or
`[github].publish = true` enables report comments and commit statuses using your
`gh` login. The separate `publish-issues` command creates issues or reuses matching
existing issues on request; it does not update their bodies. These GitHub actions
are distinct from model-provider traffic and the
network access used by builds, dependencies, and your configured backend.

## Local resources

- Two device sessions (simulator + emulator), an Xcode build, a Gradle build and native suites run on your Mac.
  Peak memory during a parallel run was ~14 GB on the reference host (48 GB); `[scheduler]` defaults allow
  one functional run per 24 GB of RAM and refuse to start when free memory or load is out of bounds.
- Disk: each run keeps its worktree, DerivedData and evidence (a few hundred MB to a few GB). Retention is
  yours to set: `codeandconfirm gc --older-than-days N` (default 14 in docs; `[artifacts].retention_days`).
- Simulators and emulators are reused across runs; app data is cleared per run.

## Data handling

Local storage does not mean local inference.

| Destination | What to expect |
|---|---|
| Your Mac | Run worktrees, diffs, reports, screenshots, logs, and worker transcripts are stored locally. |
| Model provider | Workers send their prompts and selected context for inference. This can include source code, diffs, command output, accessibility text, screenshots, and test data visible in the app. Codex uses its configured provider; Claude QA uses Claude's service. |
| GitHub | Opt-in publication sends report text and commit status, or finding text through `publish-issues`. CodeAndConfirm does not upload screenshot files through these commands. Report text can still contain sensitive details. |
| Build tools and backend | Dependency downloads, app requests, and configured commands use their own network connections. A local-emulator configuration does not make the whole run offline. |

Reports and coordinator command summaries apply pattern-based redaction for common
token formats. This is not a guarantee that all artifacts or model inputs are
sanitized. Screenshots are not automatically redacted, and raw worker transcripts
can contain sensitive content. Use synthetic accounts and non-production data;
inspect reports and images before publishing or committing them.

Provider retention and training policies depend on the service, account, and
settings you use. CodeAndConfirm does not override those policies. See
[trust boundaries](trust-boundaries.md) for the worker's local permissions.
