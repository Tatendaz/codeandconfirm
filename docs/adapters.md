# Adding platforms and computer-use adapters

CodeAndConfirm separates three things: **target adapters** (how to build/install/test a repository — pure
configuration), **device adapters** (how to read and drive a device), and the **QA worker** (a Codex role that
uses both through a shell).

## Target adapters (configuration only)

A repository is described by `codeandconfirm.toml`. To support a new app you need no code:

- `[platforms.<name>]` with `build`, the artifact path (`app` for iOS, `apk` for Android) and identifiers.
- `[suites.<name>]` with `command`, `platform`, and `results = { kind = xcresult|junit|junit-dir|none, path }`.
  `none` means the gate can only use the exit code (documented limitation; prefer a parsed artifact).
- `[backend]` with `kind = command|firebase-emulator|none`, `start`, `health`, `ports`, `port_isolation`.
- `[journeys.catalog]` in plain language; `[qa].required_journeys` picks the mandatory ones.

Commands run from the isolated checkout with `{placeholders}` rendered by the coordinator. Anything the build
legitimately rewrites inside the checkout must be restored before the gate (the pristine check fails on
tracked-file changes; the coordinator whitelists the files it rewrites itself, such as `firebase.json`).

## Device adapters (code)

`codeandconfirm/device/base.py` defines `Device`. Implement the abstract primitives:

| Primitive | iOS (`ios_idb.py`) | Android (`android_adb.py`) |
|---|---|---|
| `tree()` → `Element[]` | `idb ui describe-all` (points) | `uiautomator dump` (pixels) |
| `screenshot(path)` | `simctl io screenshot` (idb fallback) | `screencap -p` |
| `tap_xy`, `type_text`, `key`, `swipe` | `idb ui tap/text/key/swipe` | `input tap/text/keyevent/swipe` |
| `launch/terminate/install/uninstall/clear_data` | `simctl` | `am start -W`, `am force-stop`, `adb install`, `pm clear` |
| `app_state()` → foreground + installed identity | container Info.plist + executable sha256 | `pm path` + `sha256sum`, `dumpsys activity` |
| `logs()`, `keyboard_shown()`, `dismiss_keyboard()`, `memory_kb()` | `log show`, tap neutral label, `ps rss` | `logcat --pid`, `dumpsys input_method`, `dumpsys meminfo` |

iOS specifics learned on the reference host (iOS 26 simulator, idb 1.5): `describe-all` omits navigation-bar and
toolbar buttons for SwiftUI apps, so `IOSSimulator.bar_elements()` hit-tests points along the bar rows with
`describe-point` and `find()` falls back to it; (re)installing an app can wedge the simulator's accessibility
server (`recover_accessibility()` restarts `backboardd`, then reboots); `clear_data()` wipes the data container and
keychain instead of reinstalling; toggles expose the whole row, so `tap_point()` aims at the trailing edge.

Rules an adapter must keep:

1. **Device-scoped.** Take a UDID/serial; never rely on the foreground desktop. If your backend needs the
   desktop (e.g. a desktop computer-use tool), acquire the `desktop-focus` reservation
   (`scheduler.Reservation("desktop-focus", run_id, ttl)`) around every focus/keyboard/pointer operation.
2. **Accessibility first, coordinates as a controlled fallback.** `tap(needle)` resolves an element and records
   `mode: accessibility`; `--xy` records `mode: coordinate`. Never silently degrade.
3. **Evidence for every action.** Use `self._rec(...)`; screenshots go through `Evidence.screenshot_path` so the
   numbering and timestamps the gate relies on stay consistent.
4. **Build identity.** `install()` returns the artifact's hash; `app_state()` returns the installed hash; the
   coordinator compares them and the worker re-checks them. Do not skip this — it is how "testing a stale app
   already on the device" is caught.
5. **The proof must pass.** `ccdevice <platform> proof` is the contract: launch, read ≥3 elements, tap a text
   field, type, verify, dismiss keyboard, navigate, back, four screenshots.

Register the platform in `device/cli.py:make_device` and add a `[platforms.<name>]` example. Add tests in
`tests/` that exercise your parser with recorded output (no device needed).

## Desktop computer-use backends

Codex's own desktop computer-use (`cua_repl.js` → `cua.getApp("Simulator")`) is available to `codex exec`
on macOS when the user has approved the application in the Codex app. It returns an accessibility snapshot plus
screenshot and supports click/type/scroll — but it shares the one desktop. If you enable it for exploratory
checks, document it in the task prompt, hold `desktop-focus` for the whole exploratory step, and keep idb/adb
as the primary path. Claude's computer-use MCP is a separate tool and is not used by the QA worker.

## The QA worker role

`prompts/qa_role.md` is the worker's `AGENTS.md`. Change it carefully: it must keep the non-recursion rules
(no other agents/reviewers, no PR writes, no product edits), the evidence rules, and the "never inflate
coverage" rule. `prompts/verdict.schema.json` is enforced by Codex's `--output-schema`; any new field must be
added to `required` as well (strict schema).
