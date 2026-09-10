# Feature: Codex quota controls

**Branch:** feat/codex-quota-controls
**Date:** 2026-09-11

A per-change run used to start three Codex workers (review, iOS, Android) whatever the diff
touched, and the role prompt told each device worker to re-read the whole accessibility tree
after every action. One three-worker run at `high` effort came to about 0.3 M fresh input, 10 M
cached input and 35 k output tokens.

## What changed

- `qa.platforms_from_diff` (default `false`; `true` in the generated `pr` profile). A run
  without `--platforms` tests only the platforms whose product code the diff touches, using the
  same rule the gate already applies to diff scenarios: shared backend code counts for every
  platform; tests, docs, config and operations tooling count for none. A diff that touches no
  platform keeps every configured platform. The report header and the gate table
  (`platforms.not-tested`, advisory) name any platform that was skipped and why.
- The QA role prompt has a "Work economically" section: confirm each step with `wait-for`,
  `find` or `tree --grep` instead of the full tree, save screenshots without opening them
  (look at an image only for visual checks, once per screen), filter logs, read code in ranges.
  The evidence rules are unchanged.
- The report shows cached input and reasoning output tokens per worker.
- `docs/costs.md` gains a "Reducing Codex usage" table; `docs/configuration.md` documents the key.

## Notes

`static_review = false` in a project's `pr` profile is a further saving (about 15 % of a run)
where another reviewer already reads every PR; it is a project choice, not a default.
