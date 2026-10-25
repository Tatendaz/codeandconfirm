# Session: Fork PRs really run static-only

**Branch:** security/sweep-2026-10-25
**Date:** 2026-10-25

## Prompt

> You are worker sec-w5 in the 2026-10-25 security sweep. Read BRIEF.md and follow it
> exactly. Your repos: coderabbit-failover codeandconfirm model-picker.

## Steps

- Supply-chain checks, gitleaks, osv-scanner, zizmor, actionlint.
- Read the coordinator: `trust` only changed the QA worker's sandbox.
- Wrote a test that drives `execute()` with stub phases; it failed (fork ran
  backend, build, install, suites, perf). Fixed `execute()`; all 111 tests pass.
- Pinned workflow actions.
