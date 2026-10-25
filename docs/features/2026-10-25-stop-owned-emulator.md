# Feature: a run stops the emulator it booted

**Branch:** fix/stop-owned-emulator
**Date:** 2026-10-25

## Summary
When a run ends, for any reason, it now stops the Android emulator it booted. A failed boot
also stops its emulator.

## Why
Only `codeandconfirm cancel` stopped an owned emulator. A run that ended any other way (PASS,
FAIL, BLOCKED, an unexpected error) left it running. Seen on 2026-10-25: run
`20261025-185330-2eb2ba` booted `emulator-5580` and was BLOCKED one second later (port 8080
in use). The emulator ran on for 1.5 hours with no run to stop it, while the Mac was out of
memory. A boot that timed out also left its emulator running, because no run owned it yet.

## What changed
- New `stop_owned_emulators(run)` in `coordinator.py`. `Coordinator._cleanup` and
  `cancel_run` both use it. It stops only emulators the run booted. An emulator that was
  already running stays up.
- After it stops an emulator, cleanup sets the `reserving` phase back to `pending`. A later
  `resume` then reserves and boots the device again, and does not use a dead serial.
- `boot_emulator` stops the process it started when the boot fails or times out.

## Notes
The next run cold-boots the emulator (about a minute with `-no-snapshot`), unless one is
already running. Tests: `tests/test_emulator_cleanup.py`.
