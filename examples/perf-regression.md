# Worked example: a planted startup regression

Branch `cac-demo/perf-startup` adds a deliberate 1.5 s main-thread sleep at app startup on both platforms
(iOS: `Thread.sleep(forTimeInterval: 1.5)` in the App `init`; Android: `Thread.sleep(1500)` in
`MainActivity.onCreate`). It was benchmarked against `main` with:

```bash
codeandconfirm bench --branch cac-demo/perf-startup --base main
```

Run `20260906-012002-896b0d` on the reference host (Apple M5 Max, 48 GB; iPhone 17 simulator on iOS 26.5,
Android 14 emulator, `samples = 5`, budget 25 %). The phase first waited for the host to go idle
(`load 6.78 too high for a benchmark on 18 cores`), then interleaved base/candidate samples on the same devices.

| Platform | Metric | Base median | Candidate median | Δ | Threshold (max(3×MAD, 25 %)) | Verdict |
|---|---|---|---|---|---|---|
| iOS | startup_ms (launch → app prints its ready line) | 512 | 2036 | **+1524** | ±128 | **regression** |
| iOS | memory_kb (RSS, 1.5 s after ready) | 401 856 | 401 760 | −96 | ±100 464 | within noise |
| Android | startup_ms (`am start -W` TotalTime) | 3014 | 4415 | **+1401** | ±754 | **regression** |
| Android | memory_kb (PSS, 1.5 s after launch) | 179 495 | 181 822 | +2 327 | ±44 874 | within noise |

Raw samples (ms): iOS base `512 527 512 524 503`, candidate `2035 2029 2043 2036 2048`; Android base
`3049 3014 2816 3060 3007`, candidate `4490 4511 4326 4415 4394`. Verdict: **FAIL** (`perf.no-regression`),
no approval written (benchmark-only runs never approve).

What this does and does not show: the measurement is precise enough to attribute a 1.5 s injected delay with
tight variance on a simulator/emulator; it says nothing about physical-device performance, rendering hitches, or
memory growth over a session (see `docs/performance.md` for what is not implemented).
