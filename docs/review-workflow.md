# Review workflow

## Read the verdict

A report includes native-suite results, device interactions, screenshots, findings,
and untested areas. The evidence gate decides whether the configured policy passed;
a worker saying "PASS" is not enough.

| Verdict | Meaning |
|---|---|
| **PASS** | Required suites and journeys passed with evidence, build and model identity verified, candidate unmodified, no finding at or above `block_severity`. |
| **FAIL** | A required test or journey failed, a blocking defect was found, a build failed, or the candidate changed during QA. |
| **BLOCKED** | Required coverage could not be obtained, such as an unavailable device, missing evidence, or an unverifiable model. |
| **CANCELLED** | The run was cancelled. Check its cleanup status before reusing resources. |

**PASS does not mean bug-free.** The [sample report](sample-report.md) passed
its configured policy with two non-blocking findings, one medium and one low.
It also lists coverage gaps. Any new head commit needs a new verdict; approval
records include the candidate SHA, base SHA, and configuration fingerprint.

## How it works

The coordinator checks out the candidate, prepares the backend, builds and installs
the apps, runs native suites, and starts independent review, iOS, and Android workers.
The gate checks their evidence and writes the report.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="diagrams/review-pipeline-dark.svg">
    <img src="diagrams/review-pipeline-light.svg" width="100%" alt="Pipeline from a committed candidate through an isolated checkout, builds and native tests, parallel review and device QA workers, to an evidence gate and report.">
  </picture>
</p>

[Interactive diagram](diagrams/review-pipeline.html), download and open locally.

Workers operate the apps through `ccdevice`: accessibility inspection, taps, typing,
scrolling, screenshots, and logs. iOS uses `simctl` + `idb`; Android uses `adb` +
`uiautomator`. Input is device-scoped, so the two platforms can run in parallel.
Reservations and host-capacity checks limit contention. Optional desktop computer
use shares foreground focus and must be serialized.

## Costs, performance, and limits

The generated configuration includes a lighter `pr` profile and a broader `full`
profile. Runtime and token usage depend on your app and coverage.
[Profiles](configuration.md#profilesname-and---profile) and
[costs](costs.md) explain the controls and observed usage.

Performance checks compare startup time and memory with the base on an idle host.
They are preliminary simulator/emulator measurements. They do not certify
physical-device speed, frame pacing, or memory growth over a session.
[Measurement details](performance.md) and a
[detected startup regression](../examples/perf-regression.md) show the scope.

Artifacts are saved locally, but workers send selected source, UI context, and
screenshots to their model provider for inference. Review
[data handling](costs.md#data-handling) before using sensitive data.
