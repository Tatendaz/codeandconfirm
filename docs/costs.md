# Authentication and cost

## Codex

CodeAndConfirm launches `codex exec` with the binary and model you configure (`[codex].binary`, `[codex].model`).
Authentication is whatever `codex login` set up in `$CODEX_HOME/auth.json`: a ChatGPT plan login or an
`OPENAI_API_KEY`. CodeAndConfirm never reads, copies, or transmits that file; `doctor` only reports the auth mode.

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

Input tokens are dominated by re-sent context (cached); the marginal cost per step is the accessibility tree
and screenshots it reads. Set `[codex].reasoning_effort = "medium"` for cheaper per-change runs, keep
`high`/`xhigh` for release QA, and trim `required_journeys` for quick iterations.

## GitHub

`gh` is used read-only to resolve pull requests. `--publish` posts one comment and one commit status per run
using your `gh` token. No other network use.

## Local resources

- Two device sessions (simulator + emulator), an Xcode build, a Gradle build and native suites run on your Mac.
  Peak memory during a parallel run was ~14 GB on the reference host (48 GB); `[scheduler]` defaults allow
  one functional run per 24 GB of RAM and refuse to start when free memory or load is out of bounds.
- Disk: each run keeps its worktree, DerivedData and evidence (a few hundred MB to a few GB). Retention is
  yours to set: `codeandconfirm gc --older-than-days N` (default 14 in docs; `[artifacts].retention_days`).
- Simulators and emulators are reused across runs; app data is cleared per run.

## What is never sent anywhere

Screenshots, logs and diffs stay on the machine unless you `--publish` (comment text only; screenshots are
not uploaded). Reports and command logs pass through a redaction filter for common token formats before being
written.
