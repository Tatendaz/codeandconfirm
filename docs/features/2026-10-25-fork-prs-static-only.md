# Feature: Fork PRs really run static-only

**Branch:** security/sweep-2026-10-25
**Date:** 2026-10-25

## Summary

A candidate from a fork (`trust = untrusted`) now skips the backend, build, install,
suites and perf phases. Its platforms are marked BLOCKED. Only the read-only review
worker and the gate run.

## Changes

- `Coordinator.execute()` skips the five phases that run candidate code when the
  candidate is untrusted and `[trust].external_prs = static-only`.
- Each configured platform gets a `blocked` reason that names static-only.
- New test `test_fork_candidate_runs_no_phase_that_executes_candidate_code`.
- `tests.yml`: actions pinned to commit SHAs, `persist-credentials: false`.

## Why

`docs/trust-boundaries.md` said static-only runs no builds, installs, suites or device
QA. The code only made the QA worker read-only. A fork PR reviewed with
`codeandconfirm review --pr <n>` ran its own build scripts, tests and emulator functions on
the host, with the owner's credentials in reach.
