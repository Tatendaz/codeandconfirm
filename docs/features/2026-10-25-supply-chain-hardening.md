# Feature: Supply-chain hardening

**Branch:** security/hardening-2026-10-25
**Date:** 2026-10-25

## Summary

Slower, safer dependency updates, a workflow security scan, and an auto-merge job for
clean bot and security PRs.

## Changes

- `.github/dependabot.yml` (new): `github-actions` and `pip`, monthly, `cooldown:
  default-days: 7`, `open-pull-requests-limit: 3`, minor and patch grouped per ecosystem.
  Security updates ignore the cooldown.
- `.github/workflows/zizmor.yml` (new): scans `.github/workflows` on PRs and pushes to
  `main`, fails on high findings. zizmor 1.31.1 from hash-pinned wheels
  (`.github/zizmor-requirements.txt`).
- `.github/workflows/merge-when-clean.yml` (new, shared template): merges Dependabot PRs and
  the owner's `security/*` or `automerge`-labelled PRs once CI is green, a real review ran on
  the head and 0 threads are open.

## Not done

- No package-manager cooldown: pip has no minimum-release-age setting and the repo does not
  use uv. CI still installs the newest `pytest` that matches `pytest>=8`; the runner is
  GitHub-hosted with a read-only token and no secrets.
