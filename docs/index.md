# CodeAndConfirm

**One agent builds, the other tries to break it.** Claude writes the code. Codex opens your iOS and Android apps, runs user flows, and returns a verdict pinned to the tested commit.

Alpha, macOS first. A community project, not an official Anthropic or OpenAI product.

[View on GitHub](https://github.com/Tatendaz/codeandconfirm) [Set it up](#set-it-up)

## See it catch a bug

A demo branch had a planted defect: it sent a second create call, so every new tree was saved twice. Codex tested both apps, compared them with the base build, and found two stored records where there should be one. The gate returned **FAIL**.

- iOS Simulator, after an app restart, each name appears twice: [screenshot](https://tatendaz.github.io/codeandconfirm/media/duplicate-tree/ios-candidate.png)
- Android Emulator, after the base comparison and an offline check, each name appears twice: [screenshot](https://tatendaz.github.io/codeandconfirm/media/duplicate-tree/android-candidate.png)

| Platform, submission | Candidate records | Base records |
|---|---:|---:|
| iOS, single tap | 2 | 1 |
| iOS, rapid double tap | 2 | 1 |
| Android, single tap | 2 | 1 |
| Android, rapid double tap | 2 | 1 |

Report excerpt, identifiers omitted:

```text
Gate: FAIL
Candidate and base: pinned; candidate checkout unmodified
Installed identity: verified on iOS and Android
iOS smoke:         0/2 passed
Android smoke:     1/2 passed
Mock-server suite: 34/34 passed
tree.create-once:  FAILED on both platforms
Blocking findings: 3 reports of the duplicate-create defect
```

A recorded run from September 6, 2026, with synthetic names and a local Firebase emulator. The three blocking findings came from the review, iOS, and Android workers and describe the same planted defect. The demo shows detection of one injected defect, not coverage of every flow. [Base comparison and provenance](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/demo.md).

## Who it is for

Developers who let a coding agent change an iOS or Android app and want device-level evidence before they merge. It reviews a committed branch or an existing PR, and the bundled Claude skill can run it before a PR opens.

It runs on your Mac against simulators and emulators. You will need:

- Python 3.11+ and `uv`, to install the CLI
- Xcode and `idb`, for the iOS Simulator
- Android SDK and your app's JDK, for the Android Emulator
- A signed-in Codex CLI with access to your configured model
- Optional: Claude Code for the coding-agent workflow, and `gh` for PRs

## Set it up

The quickest path is to hand setup to your agent. The [agent setup guide](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/agent-setup.md) covers discovery, device setup, configuration, Claude integration, and verification.

Give this to your agent:

```text
Set up CodeAndConfirm for this app repository. Read and follow
https://github.com/Tatendaz/codeandconfirm/blob/main/docs/agent-setup.md.
Inspect the project, install missing tools with my approval, configure QA, and verify a first run.
Preserve existing settings. Ask me for logins or decisions you cannot infer. Do not merge or publish anything.
```

### Install by hand

Tested on Apple silicon with iOS Simulator and Android Emulator. [Tool setup](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/quickstart.md#1-install).

```bash
git clone https://github.com/Tatendaz/codeandconfirm.git
cd codeandconfirm
uv tool install .
codeandconfirm --version
```

### Configure your app repository

From the app repository you want to test:

```bash
cd /path/to/your/app-repo
codeandconfirm init --devices
```

Edit the generated `codeandconfirm.toml` for your app's build commands, artifacts, identifiers, suites, backend, and required journeys. Then run `codeandconfirm doctor`. This setup is required. [Walkthrough](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/quickstart.md), [example config](https://github.com/Tatendaz/codeandconfirm/blob/main/examples/firebase-two-app.toml).

### Review a branch or an existing PR

Commit your change first. Create `criteria.md` with the behaviors to check:

```markdown
- Creating a tree produces exactly one row and one backend record.
- Rapid Create taps do not create duplicates or open an older tree.
- Created data survives an app restart.
```

From the configured app repository, choose one:

```bash
# Current branch
codeandconfirm review --branch "$(git branch --show-current)" --base main --criteria-file criteria.md

# Existing PR, replacing the example URL
codeandconfirm review --pr https://github.com/owner/repo/pull/123 --criteria-file criteria.md
```

Both use isolated worktrees. Add `--merge-candidate` to test a PR's merge candidate, or `--publish` to post the report and commit status. Reports stay local by default. Use `codeandconfirm report <run-id>` to read the result.

### Let Claude request QA

Install the Claude skill first: agent-led setup does it, or follow the [manual skill installation](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/quickstart.md#6-from-claude-code). The skill runs the repair loop. Tell Claude:

```text
Implement this change, then use /codeandconfirm before opening the PR.
Fix blocking findings and rerun QA. Stop after five repair cycles. Do not merge.
```

For enforcement, add the [pre-PR hook](https://github.com/Tatendaz/codeandconfirm/blob/main/hooks/pre-pr-gate.sh) and the [server-side check](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/server-side-check.md). [Codex can also be the lead](https://github.com/Tatendaz/codeandconfirm/blob/main/skills/codeandconfirm/for-codex-lead.md).

Trusted-candidate QA runs unsandboxed and executes build scripts. Read the [trust boundaries](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/trust-boundaries.md) first. Fork PRs get static-only review by default.

## How it works

The coordinator checks out the candidate, prepares the backend, builds and installs the apps, runs native suites, and starts independent review, iOS, and Android workers. The gate checks their evidence and writes the report. [Workflow and verdict rules](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/review-workflow.md).

**Devices, driven by Codex.** Workers operate the apps through `ccdevice`: accessibility inspection, taps, typing, scrolling, screenshots, and logs. iOS uses `simctl` and `idb`; Android uses `adb` and `uiautomator`. Input is device-scoped, so both platforms can run in parallel.

**An evidence gate.** The gate checks native-suite results, required journeys, build identity, model identity, and evidence. A worker saying "PASS" is not enough.

- **PASS**: required suites and journeys passed with evidence, identities verified, no finding at or above `block_severity`.
- **FAIL**: a required test or journey failed, a blocking defect was found, a build failed, or the candidate changed during QA.
- **BLOCKED**: required coverage could not be obtained, such as an unavailable device or missing evidence.
- **CANCELLED**: the run was cancelled.

**Pinned to one commit.** Approval records include the candidate SHA, base SHA, and configuration fingerprint. Every new commit needs a new verdict.

## Status and limits

- **Alpha, version 0.1.0.** macOS first. Tested on Apple silicon with iOS Simulator and Android Emulator. Physical devices are not supported yet.
- **PASS is not bug-free.** The [sample report](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/sample-report.md) passed its policy with two non-blocking findings and lists coverage gaps.
- **Your code reaches a model provider.** Artifacts stay local, but selected code, UI context, and screenshots go to the provider. [Costs and data handling](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/costs.md).
- **Performance checks are preliminary.** Startup and memory comparisons run on an idle host. They do not certify physical-device speed or frame pacing. [Limits](https://github.com/Tatendaz/codeandconfirm/blob/main/docs/performance.md).

## Built by Tatenda Zhou

- Site: https://tatendaz.github.io/
- Follow on X: https://x.com/realtatendazhou
- LinkedIn: https://www.linkedin.com/in/tatendazhou
- Need help making AI coding agents reliable? tatendaz@me.com

---

HTML version: https://tatendaz.github.io/codeandconfirm/ · Source: https://github.com/Tatendaz/codeandconfirm · Apache-2.0 · Agent guide: https://tatendaz.github.io/llms.txt
