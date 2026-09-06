# Troubleshooting

Start with `codeandconfirm doctor`. Then `codeandconfirm status <run-id>` shows which phase stopped and
why; `report <run-id>` lists every gate check with its detail.

## BLOCKED verdicts

| Symptom in report | Cause | Fix |
|---|---|---|
| `codex: no Codex binary lists model gpt-6-astra` | CLI too old for the model | `codex update` (0.153+), or set `[codex].binary` |
| `worker.<x>.model-verified: no session rollout found` | Codex ran with `--ephemeral` or `CODEX_HOME` differs | remove `--ephemeral` from `extra_args`; set `CODEX_HOME` for both shell and coordinator |
| `backend: base ports already in use` | a dev emulator/mock server holds 9099/8080/… | stop it, or switch to `port_isolation = "per-run"` once both apps accept port overrides |
| `device unavailable: simulator not booted` | simulator shut down / runtime missing | `codeandconfirm init --devices`; `xcrun simctl list runtimes` |
| `emulator … did not finish booting` | first cold boot on a slow host, or no HVF | check `artifacts/emulator.log`; try `--headless-android`; increase RAM in the AVD |
| `adapter.ios.proof: no text field on screen` | the app did not reach its first screen (crash / wrong launch args) | `ccdevice ios logs --since 2m`; check `platforms.ios.launch_args` |
| `suite.<name>: no parseable test results` | the runner never produced the artifact (build error, wrong `-only-testing` id) | open `artifacts/suite-<name>.log` |
| `worker.<x>.completed: timed out` | QA took longer than `worker_timeout_minutes` | raise it, or narrow `required_journeys` |
| `evidence.<p>.hands-on: N interactions (min M)` | the worker read code instead of driving the app | see its `events.jsonl`; this is the gate doing its job |
| `journey.<id>.<p>: marked passed but cites no existing evidence file` | evidence names do not match files in `evidence/<p>/` | the worker must cite file names as written by `ccdevice` |
| `host capacity not available` | load or memory over the scheduler thresholds, or CI busy | wait, or tune `[scheduler]` (do not benchmark during CI) |
| `repair budget exhausted` | more than `max_repair_cycles` FAIL→fix cycles on one branch | a human reviews; `--reset-repairs` only after that review |

## FAIL verdicts that look wrong

- **`candidate.unmodified: tracked files changed during QA`** — something wrote into the checkout. If it was
  a build step of yours (generated file committed to git), add its path to a `prepare` step that restores
  it, or fix the build. If it was the worker, read its events: it violated its role.
- **A suite failed but passes locally** — the suite ran against the run's isolated checkout and backend.
  Compare `artifacts/suite-*.log` with your local command; port or fixture differences are the usual cause.
- **Blocking finding without evidence (`concrete: false`)** — still FAIL by design (a narrative can lower a
  grade but never raise it). Ask for a re-run after the worker's evidence rules are met, or triage manually.

## Devices

- iOS typing drops characters → the adapter waits 0.6 s after a focusing tap; slow hosts may need more
  (`Device.settle_s`). Autocapitalization can change the first letter; compare case-insensitively.
- iOS `tree` returns only the `Application` node although the screenshot shows UI → the simulator's accessibility
  bridge is wedged. Trigger observed on the reference host: **(re)installing an app** while the bridge is in use
  (XCUITest sessions reinstall too); afterwards even Settings reports nothing. This is why `clear-data` wipes the
  data container + keychain instead of reinstalling. `ccdevice ios
  recover-accessibility` restarts the idb companion, reboots the simulator and relaunches the app; the coordinator
  does this automatically after the native suites and the proof retries once after recovery.
- iOS navigation-bar / toolbar buttons are missing from `ccdevice ios tree` (idb `describe-all`, flat and nested, omits
  them on iOS 26). Use in-content controls, or `ccdevice ios scale` + a screenshot to derive `--xy` points and
  `ccdevice ios describe-point X Y` to confirm what is there. Coordinate taps are recorded as `mode: coordinate`.
- iOS `back` does nothing → there was no navigation bar back button; the adapter falls back to an edge swipe.
- Android taps land on the keyboard → run `ccdevice android dismiss-keyboard` before tapping controls near
  the bottom; the proof does this automatically.
- Android `input text` cannot type non-ASCII; use `ccdevice android type` for ASCII only.
- `uiautomator dump` fails while an animation runs → the emulator is set to animation scale 0 at boot; if
  you booted it yourself, run `adb shell settings put global animator_duration_scale 0`.

## Cancellation and cleanup

- `codeandconfirm cancel <run-id>` sets the flag, SIGTERMs the coordinator (which kills its workers and
  suites), stops the backend it started, releases its locks, and shuts down an emulator it booted.
- `codeandconfirm locks --clear-stale` removes reservations whose owner process is gone.
- `codeandconfirm gc --older-than-days 14` removes old runs and their worktrees.
- Dedicated devices are reused across runs; delete them with `xcrun simctl delete "CodeAndConfirm iPhone"`
  and by removing `~/.android/avd/codeandconfirm_api34.*`.

## Where things are

```
$CODEANDCONFIRM_HOME/            (default ~/.codeandconfirm)
  runs/<run-id>/run.json         state machine, phases, pids, repair cycle
  runs/<run-id>/ctx.json         everything the gate evaluated
  runs/<run-id>/report.md        the report
  runs/<run-id>/artifacts/       build/suite/backend logs, commands.jsonl, xcresult, junit
  runs/<run-id>/evidence/<w>/    screenshots, actions.jsonl, build-identity.json, proof.json
  runs/<run-id>/codex/<w>/       AGENTS.md (role), task.md, prompt.md, events.jsonl, result.json
  approvals/<repo-key>/<sha>.json
  locks/  repairs/  cache/spm
```
