# Agent setup instructions

For an agent asked to install CodeAndConfirm in an existing app repository.
Carry out the setup, verify it, and report what works. Do not stop at a plan or
ask the user to copy commands you can run yourself.

This guide is an installation checklist, not an automatic installer or a grant
of unrestricted permissions. Follow the user's instructions and host approval
rules. Read the linked files from the same CodeAndConfirm revision as this guide.

## 1. Inspect before changing anything

- Identify the target app repository from the current task. If ambiguous, ask for
  its path. This is the app to test, not the CodeAndConfirm source repository.
- Read its agent instructions, README, build scripts, CI, and existing QA config.
  Check Git status and preserve all existing work. Use an isolated worktree when
  the project's rules require one; never reset or stash someone else's changes.
- Identify the platforms actually present, build variants, app identifiers,
  existing smoke suites and result formats, backend, and important user journeys.
  Do not invent paths or assume every app resembles the Firebase example.
- Confirm this agent can run commands on the user's Mac. Cloud-only agents cannot
  operate a local iOS Simulator without an explicitly configured remote connection.
  If the host is unsupported, report that before installing mobile tools.
- Inspect existing CLI versions, skill links, hooks, local overrides, device
  reservations, and available disk/RAM. Reuse correct installations.

Ask only for missing decisions that change the setup: repository path, supported
platforms, backend choice, model access, install location, or an acceptable QA
time/usage budget. Default to Claude as lead and Codex as QA unless told otherwise.

## 2. Agree the safety boundaries

Read [trust boundaries](trust-boundaries.md) and [data handling](costs.md#data-handling).
Explain before the first QA run that trusted candidates execute build scripts and
device workers unsandboxed, and selected source/UI context reaches the model provider.
Get approval for that execution and its usage budget before launching workers.

Use synthetic accounts and a local backend for initial setup. Do not use production
credentials or enable a real cloud backend implicitly. Keep fork PRs static-only.
Never read or print token files, copy credentials into the repo, accept license
agreements, buy access, or complete login flows on the user's behalf. Ask the user
to authenticate through the relevant CLI when needed.

Do not disable old review loops, change global hooks or branch protection, schedule
jobs, publish comments/issues, push, or merge as part of default setup. Offer those
as separate opt-ins after verification. Preserve existing CI and human review.

## 3. Install what is missing

Use the [quickstart](quickstart.md#1-install) for requirements and the current
commands. Inspect tools first; request approval for missing software, SDK/runtime
downloads, system changes, or replacing an existing installation. Use official
distribution sources and respect the project's required Xcode/JDK versions.

Choose a persistent CodeAndConfirm clone location with the user. If a clone
already exists, check its remote, revision, and dirty state before reusing it.
Use the user's requested trusted revision, or the default branch when none was
specified. Before installation, require a clean checkout at that trusted revision.
If it is dirty or points to an unapproved revision, stop and ask for explicit
approval before installing from it. Do not reset, overwrite, or upgrade it silently.

From the selected CodeAndConfirm clone:

```bash
uv tool install .
codeandconfirm --version
ccdevice --help
```

If the commands are not on PATH, inspect the tool-bin location and follow the
quickstart's shell setup. Verify both commands in the environment that will run
the coding agent, not just an interactive terminal.

Check Xcode/runtime/idb for iOS and Android SDK/system image/JDK for Android.
Have the user sign into the coding and QA CLIs. Use `codeandconfirm doctor` to
verify the configured model; if unavailable, ask the user to select an available
model rather than substituting one silently. `gh` authentication is needed for PRs.

## 4. Configure this app

From the target app repository, create templates without creating devices yet:

```bash
codeandconfirm init
```

Do not use `--force`. Existing config files must be inspected and merged carefully.
`init` can also create a machine-wide config if one is absent. It does not add the
local override to `.gitignore` for you; ensure `.codeandconfirm.local.toml` is ignored.

Read the [configuration reference](configuration.md) and adapt the generated TOML:

| Area | What to configure from observed project files |
|---|---|
| Platforms | `[qa].platforms` and the matching `[platforms.*]` entries; only platforms the app supports |
| Builds | Real build commands, artifact paths, bundle/package IDs, launch arguments, and simulator-compatible variants |
| Suites | Existing smoke commands, parsable results such as xcresult/JUnit, and required suite names |
| Journeys | App-specific descriptions and required IDs, including creation/editing, navigation, errors, and persistence where relevant |
| Backend | A local test backend, start/health commands, and reserved ports; `none` only for apps that need no backend |
| Profiles | Inspect effective `pr` and `full` overrides; changing a top-level key does not override a profile |
| Local settings | Machine paths and device IDs in ignored overrides; secrets outside tracked files |

The example includes family-tree-specific journeys and platform settings. Replace
them. If an app has no suite or a required journey cannot run, report the coverage
gap and ask before adding tests; do not hide it with an empty required list.
Do not copy a `full` profile that switches to a real backend into first-run setup.

Keep the existing blocking threshold and required checks unless the user explicitly
chooses a different policy. Use a bounded first-run timeout. Enable device-scoped
iOS/Android parallelism when the host has capacity; keep reservation and load
checks. Serialize desktop focus and performance measurements. Do not promise that
simulator results certify physical-device performance.

After selecting dedicated device names, compatible runtimes, and unused ports:

```bash
codeandconfirm init --devices --boot
codeandconfirm config
codeandconfirm doctor
```

Inspect effective config locally, including profile overrides, without pasting
secrets into chat or reports. Resolve failing checks; never kill another run or
erase a personal device to free a resource.

## 5. Connect the coding agent

For Claude Code, read [the QA skill](../skills/codeandconfirm/SKILL.md), then install
it using [quickstart step 6](quickstart.md#6-from-claude-code). Prefer a project-local
copy in `.claude/skills/codeandconfirm` so unrelated repos are unaffected. A
machine-wide symlink is also supported with user approval. Check existing files
and links; do not overwrite them. Re-running setup must not add duplicate entries.

For Codex as lead, use [the Codex lead instructions](../skills/codeandconfirm/for-codex-lead.md)
and the role configuration instead. Other agents can invoke the CLI, but do not
claim they have a native integration. Claude Agent Teams is not a prerequisite;
the coordinator launches the QA workers.

The skill guides behavior. If the user wants local enforcement, explain and offer
the [pre-PR hook](../hooks/pre-pr-gate.sh); merge an approved entry into existing
settings rather than replacing them. Do not enable it before a first verdict is
available. Keep the old loop until a separate [migration](migration.md) is approved.

## 6. Verify one real run

Confirm the chosen candidate is committed. Do not commit unrelated work. Write
3–8 app-specific acceptance criteria to a temporary file. Check the chosen profile's
required suites and journeys with the user before starting the approved QA run.

From the configured app repository, choose one command and replace placeholders:

```bash
codeandconfirm review --branch <committed-branch> --base <base-ref> --criteria-file <criteria-path>
codeandconfirm review --pr <PR-URL-or-number> --criteria-file <criteria-path>
```

Do not pass `--publish`; also verify effective `[github].publish` is false.
Use `status <run-id>` while it runs and `report <run-id>` afterward. A successful
`doctor` is not evidence that the app was built or tested. Inspect native-suite
results, installed identity, model verification, actions, screenshots, each required
journey, and coverage limits. Never invent a PASS or reuse an unrelated run.

For FAIL, distinguish a product finding from setup failure. Report the finding and
ask before expanding installation into product repairs. For BLOCKED, fix in-scope
configuration problems or report the exact missing access/tool. Never weaken the
gate to complete setup. Use `cancel <run-id>` to stop only a run you own if needed.

## 7. Hand off with evidence

Report the installed revision and CLI paths, changed files, selected model/profile,
devices, backend, and whether a skill or optional hook was installed. Include the
first run ID, candidate SHA, report path, verdict, findings, and untested areas.
Distinguish **configured but unverified**, **QA executed with FAIL/BLOCKED**, and
**QA verified PASS**. Provide the exact next branch and existing-PR commands for
this app. List any remaining user action and how to undo the setup changes.

Leave the configuration diff for review unless committing was explicitly authorized.
Do not call the app regression-free because installation succeeded.
