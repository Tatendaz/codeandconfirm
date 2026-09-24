# Session: Codex quota controls

**Branch:** feat/codex-quota-controls
**Date:** 2026-09-11

## Prompts

1. "The last time I ran CodeAndConfirm on one of my branches it ate a lot of Codex quota.
   Are there any optimizations we can do to avoid this? I am open to any suggestions."
2. "Do A and B. Let's set effort to low for Astra, for [my app's] runs."

## Work

Read the per-worker token usage of the last eighteen runs. Input tokens are almost all re-sent
context; the Android worker at `high` effort ran 319 commands where `medium` workers ran 30–60.
Option A was configuration only (effort, static review, timeouts) in the target app's profile.
Option B was this change: diff-based platform selection with the skip named in the report and
gate table, a leaner role prompt, cached/reasoning token counts in the report, and docs.
Ten new tests cover the selection rules, the gate line, the report line and the prompt.

## Decisions

- A diff that touches no platform falls back to every configured platform rather than
  testing nothing: the tool narrows coverage only when the change clearly belongs to one side.
- Screenshots stay mandatory evidence; the worker is only told not to open them routinely.
