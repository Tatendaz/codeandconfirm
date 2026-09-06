# Trust boundaries

CodeAndConfirm runs code you did not write (the candidate) on your machine, and lets an AI worker
drive real simulators and emulators. Read this before pointing it at a pull request.

## What the QA worker can do

For a **trusted** candidate (your own branch, or a PR from the same repository), the Codex worker runs
with `--dangerously-bypass-approvals-and-sandbox`. This is not optional: `xcrun simctl`, `adb`, and
`idb` cannot reach their daemons from inside Codex's seatbelt sandbox (verified: CoreSimulatorService
"connection invalid", adb "could not install smartsocket listener"). Unsandboxed means the worker
can run any command your user can run. Mitigations that are in place:

- The worker's role forbids product edits, pushes, PR/issue writes, launching other agents, and
  reading credentials; the gate fails the run if tracked files in the checkout change.
- The worker only sees device ids for its own platform; the coordinator installs the build.
- Every command the coordinator runs is recorded with its exit code; Codex's own commands are in
  `codex/<worker>/events.jsonl` and the Codex session rollout.
- Secrets are redacted from reports and command logs by pattern (tokens, keys, JWTs, PEM blocks).

None of that stops a malicious *candidate*. A PR can contain a build script, a test, or a Gradle
plugin that executes arbitrary code during `xcodebuild`/`gradlew` — before the worker even starts.

## External-contributor PRs

`[trust].external_prs` controls PRs whose head lives in a fork (`isCrossRepository`):

- `static-only` (default): the review worker runs **read-only sandboxed** and no builds, installs,
  suites or device QA run. Device coverage is reported as **BLOCKED**, never as tested.
- `refuse`: the run ends BLOCKED immediately.

To test an external PR hands-on you need isolation the local host cannot give you:

1. A disposable macOS VM or a dedicated CI Mac with **no** developer credentials, no `gh` token with
   write scope, no Firebase service accounts, no signing identities, and no access to your normal
   `~/.codex` (log in with a QA-only account).
2. Run `codeandconfirm review --pr <n>` there with a copy of the project config; publish the status
   from that machine.
3. Wipe the VM after the run.

Do not weaken `external_prs` to get device coverage on your workstation.

## What "PASS" means, and what it does not

A PASS approval record is a local JSON file under `$CODEANDCONFIRM_HOME/approvals/` bound to the exact
candidate SHA, base SHA, configuration fingerprint and required plan. It is written by the same user
that could edit it. It proves that *this machine* ran the plan and the gate accepted the evidence;
it is not tamper-proof. Use the server-side status check (`examples/github/codeandconfirm-required.yml`, ideally with its
`CODEANDCONFIRM_STATUS_CREATOR` variable pinned to the publishing identity,
in this repo, copied into the target repository) as the merge backstop, and treat the commit status
as "a run happened for this SHA from a machine with the developer's token".

## Credentials

- Codex authentication stays in `$CODEX_HOME/auth.json`; CodeAndConfirm never copies it.
- `gh` resolves PRs read-only by default. `--publish` or `[github].publish = true`
  enables report comments and commit statuses; `publish-issues` separately opts
  into creating or updating issues.
- The backend used for QA is a **local emulator** unless your config says otherwise.
  Never point the QA config at production. Optional [real-development-backend runs](real-backend.md)
  require separate credentials and cleanup configuration.
- Test accounts are synthetic (`cac-<run>-<platform>-<n>@example.test`).

Worker-selected code, screenshots, and other context reach the model provider even
when GitHub publication is disabled. Local artifact storage is not an offline or
local-inference guarantee. See [data handling](costs.md#data-handling).

## Desktop computer use

The device adapter uses idb/adb, which are device-scoped and need no desktop permission. Codex's own
desktop computer-use (`cua_repl`) requires per-application approval inside the Codex app and shares
the single foreground desktop; it is optional and, if enabled, must be serialized with the
`desktop-focus` reservation. Do not grant it "all applications".
