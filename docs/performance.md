# Performance measurements

CodeAndConfirm does not promise "no regressions". It measures, compares against the base commit under the
same conditions, and reports what it could not conclude.

## What is implemented (`[perf].enabled = true`)

- **Base build**: the base SHA is checked out into its own worktree and built with the same command,
  toolchain and DerivedData layout as the candidate.
- **Same device, interleaved samples**: base and candidate builds are installed alternately on the same
  simulator/emulator; each sample is a cold start after `terminate` (N = `[perf].samples`, default 5).
- **Metrics**:
  - `startup_ms` — Android: `am start -W` `TotalTime`. iOS: with `[perf].ios_ready_log_marker` set, the app is
    launched with `simctl launch --console-pty` and the time from the launch call to the app printing that
    line is measured (±20 ms jitter observed); without a marker, the accessibility snapshot is polled every
    100 ms until interactive elements appear. Both are host-measured and comparable only between builds
    measured the same way on the same device.
  - `memory_kb` — Android `dumpsys meminfo` TOTAL PSS; iOS RSS of the simulated process, ~1.5 s after launch.
- **Comparison**: medians. The noise floor is `max(3 × MAD(base), base_median × budget%)` with
  `budgets.startup_ms_percent` (default 25). Beyond the floor: `regression` (fails the gate) or `improvement`;
  inside: `within noise`. Fewer than three valid samples per side → `inconclusive` (reported; blocking only
  when `[perf].required = true`).
- **Exclusivity**: the phase refuses to run when CI workers are busy on the host or the 1-minute load is above
  35% of the cores, and reports that instead of measuring.
- **Artifacts**: raw samples in `artifacts/perf.json`; the report lists each metric with base/candidate medians,
  delta and threshold.

## What is not implemented

- Journey timings beyond startup (open large tree, navigation, rendering responsiveness), memory growth over a
  session, frame-time/hitch measurements, and profiler traces (Instruments / Perfetto). These need per-app
  journey scripts; the adapter (`ccdevice … wait-for`) provides the primitives.
- Physical devices. Simulator/emulator numbers are **preliminary signals**; the report labels them as such.
  Support for physical devices means: a `device_class = "physical"` platform entry using `xcrun devicectl` /
  `adb -s <usb-serial>`, and reports that separate the two classes. Not done yet.

## Demonstrating a detected regression

The repository's `examples/perf-regression.md` describes the check we ran while building this project: a
candidate that adds a deliberate sleep to app startup, measured against its base with the procedure above.
Results and limitations are recorded there rather than claimed here.
