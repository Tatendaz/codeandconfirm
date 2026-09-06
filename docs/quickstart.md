# Quickstart

## 1. Install

```bash
git clone https://github.com/Tatendaz/codeandconfirm.git
cd codeandconfirm
uv tool install .
codeandconfirm --version
```

iOS tooling: Xcode + `brew tap facebook/fb && brew install idb-companion && uv tool install fb-idb --python 3.12`.
Android tooling: Android SDK (`ANDROID_HOME`), one arm64 system image, JDK 17 (`brew install openjdk@17`).
Codex: `codex login`, then `codex debug models | grep -o '"slug":"[^"]*"'` must list the model you configure.

Keep this clone to link the Claude skill in step 6. Contributors who need an editable
install should use the [development instructions](../CONTRIBUTING.md#development).
If your shell cannot find the installed commands, run `uv tool update-shell` and open
a new terminal.

## 2. Configure your app repository

```bash
cd /path/to/app
codeandconfirm init --devices --boot
```

This writes `codeandconfirm.toml` (commit it; placeholders only), `.codeandconfirm.local.toml` (gitignore it),
creates a dedicated simulator ("CodeAndConfirm iPhone") and AVD (`codeandconfirm_api34`), and boots the
emulator. Edit `codeandconfirm.toml`:

- `[platforms.ios].build` / `app` / `bundle_id` / `launch_args` — how to build and where the `.app` lands.
- `[platforms.android].build` / `apk` / `package` / `activity`.
- `[suites.*]` — the established suites the coordinator must run, with a `results` artifact the gate can parse
  (`xcresult`, `junit`, `junit-dir`). Prefer a smoke subset for per-change QA and keep the full suite in CI.
- `[backend]` — a local backend (Firebase emulator, mock server) and its health URLs.
- `[qa].required_journeys` + `[journeys.catalog]` — the hands-on core journeys, in plain language.

Placeholders you can use: `{run_dir} {checkout_dir} {artifacts_dir} {cache_dir} {ios_udid} {android_serial}
{auth_port} … {jdk17_home} {jdk21_home} {android_sdk} {candidate_sha} {base_sha}`.

Then `codeandconfirm doctor`. Fix anything marked FAIL.

## 3. Prove the adapter on your build (once)

```bash
# after a manual build + install, or let a review do it:
CAC_IOS_UDID=<udid> CAC_IOS_BUNDLE=com.example.app CAC_EVIDENCE_DIR=/tmp/ev ccdevice ios proof
CAC_ANDROID_SERIAL=emulator-5580 CAC_ANDROID_PACKAGE=com.example.app CAC_EVIDENCE_DIR=/tmp/ev2 ccdevice android proof
```

The proof launches the app, reads its accessibility tree, taps a text field, types, verifies the value, dismisses
the keyboard, navigates to another state and back, and saves four screenshots. Every review runs it before
handing a device to the QA worker; a failing proof makes that platform BLOCKED.

## 4. Review a branch

Start from a branch with your change committed. Create `criteria.md` in the app
repository with acceptance criteria appropriate to your change. For this example:

```markdown
- Creating a tree from the empty state produces exactly one tree.
- Sign-in errors are shown inline; no raw HTTP codes.
```

```bash
codeandconfirm review --branch "$(git branch --show-current)" --base main --criteria-file criteria.md
```

Watch progress in another terminal: `codeandconfirm status <run-id>`. When it ends:

```bash
codeandconfirm report <run-id>          # Markdown report
codeandconfirm gate-check --sha HEAD    # exit 0 when a current PASS exists
```

## 5. Review an existing pull request

```bash
codeandconfirm review --pr https://github.com/owner/repo/pull/123            # local report only
codeandconfirm review --pr 123 --merge-candidate                              # test the merge commit instead of the head
codeandconfirm review --pr 123 --publish                                      # + PR comment + commit status
```

Head and base SHAs come from `gh`; both are fetched into run-private refs (`refs/codeandconfirm/*`) and
checked out into the run's own worktree. Your working copy is never modified. The report records whether the
**head** or a **merge candidate** was tested, and PRs from forks are treated as untrusted (static review only).

## 6. From Claude Code

Install the skill: symlink or copy `skills/codeandconfirm` into `~/.claude/skills/` (machine-wide) or into the
target repository's `.claude/skills/` (ships with the project). Claude will request QA before opening a PR, read
the verdict, fix blocking findings, and re-run — at most five repair cycles per branch. When Codex is the lead
(`[roles] lead = "codex"`), paste `skills/codeandconfirm/for-codex-lead.md` into `AGENTS.md` instead.
Optionally install `hooks/pre-pr-gate.sh` as a `PreToolUse` hook so `gh pr create` / `git push` are blocked
without a current PASS for HEAD.

From the CodeAndConfirm clone, if the destination does not already exist:

```bash
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/codeandconfirm" ~/.claude/skills/codeandconfirm
```

Then ask Claude to use `/codeandconfirm` in the configured app repository before
opening the PR. The [README workflow](../README.md#let-claude-request-qa) includes
a copyable prompt.

## 7. Resume, cancel, clean up

```bash
codeandconfirm resume <run-id>     # continue after a terminal interruption (same config snapshot)
codeandconfirm cancel <run-id>     # stop workers/suites/backend it owns, release reservations
codeandconfirm locks               # what is reserved, by which run
codeandconfirm gc --older-than-days 14
```
