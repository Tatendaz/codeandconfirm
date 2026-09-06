# CodeAndConfirm

**One agent builds, the other tries to break it.**

CodeAndConfirm is a small, independent coordinator for a two-agent workflow: **Claude Code implements
a change; an OpenAI Codex worker (Astra when available) independently reviews and tests it** — on a real
iOS simulator and a real Android emulator, hands-on — before a pull request is opened or updated. A gate
then validates the evidence on disk; the worker's opinion alone never yields a PASS.

This is a community project. It is not an official Anthropic or OpenAI product.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/review-pipeline-dark.svg">
    <img src="docs/diagrams/review-pipeline-light.svg" width="100%" alt="The CodeAndConfirm review pipeline. The lead agent commits a candidate SHA and runs codeandconfirm review. The coordinator checks out that exact SHA in isolation, starts the backend, builds and installs, runs the native suites for real exit codes and parsed results, and hands the task to parallel Codex workers for review, iOS and Android, which drive the devices through ccdevice and produce screenshots, logs and evidence. The gate checks evidence, build identity, model and journeys and returns PASS, FAIL, BLOCKED or CANCELLED with report.md and a PR status.">
  </picture>
</p>

<sub>Interactive version with pan, zoom and relationship tracing: <a href="docs/diagrams/review-pipeline.html">docs/diagrams/review-pipeline.html</a> (download and open). Source: <code>docs/diagrams/review-pipeline.workflow.json</code>.</sub>

## What you get

- `codeandconfirm review --branch <b> --base <base>` or `--pr <url>` — one command, durable run state,
  `status`, `report`, `resume`, `cancel`, explicit timeouts. `codeandconfirm bench` runs only the controlled
  base-vs-candidate benchmark (startup time, memory) and never writes an approval.
- Two profiles in one config: `--profile pr` (default: sign-up plus scenarios derived from the diff, medium
  effort, about ten minutes of QA) and `--profile full` (weekly: every journey, every suite, the benchmark, high
  effort, optionally against the real development project with post-run account cleanup). See
  [docs/configuration.md](docs/configuration.md#profilesname-and---profile) and `examples/weekly/`.
- `codeandconfirm publish-issues <run-id>` (opt-in) files each finding as a GitHub issue with reproduction steps
  and evidence paths, idempotently — see [docs/publish-issues.md](docs/publish-issues.md).
- A device adapter (`ccdevice`) the QA model drives from a shell: accessibility tree, tap by label/id,
  coordinate fallback, type, keys, scroll, wait-for, screenshots, launch/restart/clear-data, logs, build
  identity. iOS via `simctl` + `idb`; Android via `adb` + `uiautomator`. Device-scoped, so iOS and Android
  run at the same time without sharing a desktop.
- A gate that independently checks: candidate SHA pinned and unmodified, installed build hash matches the
  built artifact, required native suites produced parseable results, the worker actually interacted
  (interaction count, screenshots with timestamps inside the run), every required journey has evidence,
  the model recorded in the Codex session equals the configured model, and findings at/above the blocking
  severity fail the run.
- Reservations with stale recovery, host capacity checks (memory, load, busy CI runners), configurable
  parallelism, and honest BLOCKED verdicts when a device, port, or tool is unavailable.
- A Claude skill (`skills/codeandconfirm`), an optional pre-PR hook, a server-side status check, a sample
  report, and a migration guide away from bot-comment review loops.

## Roles

The default is Claude Code as lead and a Codex worker as QA. `[roles] qa = "claude"` swaps the QA engine to
`claude -p` with the same role prompt, task and verdict schema (model verified from the session transcript);
`lead = "codex"` with `skills/codeandconfirm/for-codex-lead.md` makes Codex the implementer. Same vendor on
both sides works but `doctor` warns: an independent second opinion is the point.

## Requirements

- macOS on Apple silicon (tested on macOS 26 / Xcode 26; Intel should work but is untested).
- Python 3.11+, `uv` or `pip`.
- Xcode with an iOS simulator runtime; `idb` (`brew tap facebook/fb && brew install idb-companion`,
  `uv tool install fb-idb --python 3.12`).
- Android SDK with `platform-tools`, `emulator`, and one system image; JDK 17 for Gradle projects.
- Codex CLI 0.153+ signed in (`codex login`), able to serve the configured model (`codex debug models`).
- Optional: `gh` (PR candidates, publishing), `firebase-tools` + JDK 21 (Firebase emulator backends).

## Quickstart

```bash
git clone https://github.com/Tatendaz/codeandconfirm.git && cd codeandconfirm
uv venv .venv && uv pip install -e '.[dev]'         # or: pip install -e '.[dev]'
export PATH="$PWD/.venv/bin:$PATH"

cd /path/to/your/app-repo
codeandconfirm init --devices                        # writes codeandconfirm.toml + local override, creates devices
$EDITOR codeandconfirm.toml                          # build/install/suite commands for YOUR app (placeholders only)
codeandconfirm doctor                                # auth, model, tools, devices, ports, config

git checkout -b feat/thing && …commit…
codeandconfirm review --branch feat/thing --base main --criteria-file criteria.md
codeandconfirm report <run-id>
```

See [docs/quickstart.md](docs/quickstart.md) for a walkthrough with a two-app Firebase project, and
[examples/firebase-two-app.toml](examples/firebase-two-app.toml) for a complete configuration.

## For agents

CodeAndConfirm ships with a skill, so the agent that writes the code asks for QA on its own. Install the CLI
once per machine, then give the agent the skill.

**Claude Code as the lead (default).**

```bash
git clone https://github.com/Tatendaz/codeandconfirm.git && cd codeandconfirm
uv tool install .                                    # `codeandconfirm` and `ccdevice` on PATH; or: pipx install .
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/codeandconfirm" ~/.claude/skills/codeandconfirm   # or copy it into <your-repo>/.claude/skills/
```

With the skill in place, Claude Code runs `codeandconfirm review` before `gh pr create` and before pushing new
commits to an open PR, reads the verdict, fixes every blocking finding, re-runs, and stops after five repair
cycles. Say "run QA" or `/codeandconfirm` to trigger it by hand. To make the gate mandatory on the machine,
install `hooks/pre-pr-gate.sh` as a Claude Code `PreToolUse` hook on `Bash`: `gh pr create` and `git push` are
then refused without a current PASS for HEAD.

**Codex as the lead.** Set `[roles] lead = "codex"` and `qa = "claude"` in `codeandconfirm.toml`, and paste
`skills/codeandconfirm/for-codex-lead.md` into the repository's `AGENTS.md` (or `~/.codex/AGENTS.md`). Same
rules, Codex vocabulary.

**What the agent's machine needs** is what a human needs: the Claude Code and Codex CLIs signed in, Xcode with
`idb`, the Android SDK with JDK 17, and a `codeandconfirm.toml` in the target repository (`codeandconfirm init`).
`codeandconfirm doctor` lists what is missing, in agent-readable form.

## Verdicts

| Verdict | Meaning |
|---|---|
| **PASS** | Every required suite green with parsed results, every required journey exercised with evidence on every platform, build identity verified, model verified, no finding at/above `block_severity`, candidate unmodified. An approval record is written for this exact SHA + base + config. |
| **FAIL** | A product defect: failed suite, blocking finding, failed build, or the candidate was modified during QA. |
| **BLOCKED** | Coverage could not be obtained: device/tool/port/backend unavailable, worker timed out or produced no verdict, model unverifiable, missing evidence. Never treated as PASS. |
| **CANCELLED** | Cancelled by the user; owned resources cleaned up. |

Any new head commit needs a new verdict; approvals are per SHA and per configuration fingerprint.

## Costs and authentication

Codex runs under your Codex/ChatGPT login (or API key) — see [docs/costs.md](docs/costs.md). A full
two-platform run typically executes three workers for 10–40 minutes each at `high` reasoning effort plus
native suites; simulators, emulators and Xcode builds are the main local cost.

## Trust

Read [docs/trust-boundaries.md](docs/trust-boundaries.md) before reviewing pull requests from people you
do not trust. Device QA requires an unsandboxed worker; fork PRs get read-only static review only.

## Documentation

- [docs/quickstart.md](docs/quickstart.md) · [docs/configuration.md](docs/configuration.md)
- [docs/trust-boundaries.md](docs/trust-boundaries.md) · [docs/costs.md](docs/costs.md)
- [docs/troubleshooting.md](docs/troubleshooting.md) · [docs/sample-report.md](docs/sample-report.md)
- [docs/adapters.md](docs/adapters.md) — adding platforms and computer-use backends
- [docs/performance.md](docs/performance.md) — what is measured, and what is not
- [docs/real-backend.md](docs/real-backend.md) — running against the real development project, and the cleanup that follows
- [docs/publish-issues.md](docs/publish-issues.md) — filing findings as GitHub issues, on request
- [docs/migration.md](docs/migration.md) — replacing a bot-comment review loop
- [docs/server-side-check.md](docs/server-side-check.md) — the merge backstop

## License

Apache-2.0 (see `LICENSE`). Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

Maintained by Tatenda Zhou (<tatendaz@me.com>).
