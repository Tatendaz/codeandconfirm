# Session: Supply-chain hardening

**Branch:** security/hardening-2026-10-25
**Date:** 2026-10-25

## Prompt

> After you finish the sweep for all your repos, read HARDENING.md and do it for each of
> your repos: Dependabot config with 7-day cooldown, package-manager cooldown, CI lockdown,
> zizmor job. Add the auto-merge job from templates/merge-when-clean.yml.

## Steps

- Checked `cooldown`, `default-days`, `open-pull-requests-limit` and `update-types` in the
  SchemaStore Dependabot schema, and validated the new file against it.
- Ran actionlint and zizmor 1.31.1 locally: clean at high.
