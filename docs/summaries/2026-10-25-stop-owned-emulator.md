# Session: a run stops the emulator it booted

**Branch:** fix/stop-owned-emulator
**Date:** 2026-10-25

## Prompts
1. "68gbs is ok?" (a macOS "out of application memory" dialog that showed `qemu-system-aarch64` at 68.73 GB)
2. "find why the emulator leaked memory"
3. "fix the bug in codeandconfirm"
4. "fix the cleanup bug and merge it also via pr"

## Steps
- The 68.73 GB was the total for every process started from iTerm2. The dialog showed the same
  figure next to the emulator. The emulator itself used about 4.5 GB. The emulator was not leaking.
- Its parent was gone (ppid 1). Run `20261025-185330-2eb2ba` owned it and ended BLOCKED at the
  backend phase. `_cleanup` released locks and the backend but never stopped owned emulators.
- Added `stop_owned_emulators`, called from `_cleanup` and `cancel_run`, and a kill on failed boot.
- Tests use a real stand-in process in its own session. Both behaviour tests fail on the old code
  and pass on the new code.

## Decisions
- Stop the emulator at run end instead of keeping it warm for the next run. A warm emulator
  that no one owns is the bug.
- Reset `reserving` to `pending` when an emulator is stopped, so `resume` cannot use a dead device.
- Not run on a real emulator: the Mac was out of memory at the time.
