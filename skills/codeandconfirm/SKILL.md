---
name: codeandconfirm
description: "Request and consume independent QA from CodeAndConfirm before opening or updating a pull request. Use whenever a branch is ready for review, before `gh pr create`, before pushing new commits to an open PR, or when the user asks for QA, a verdict, or 'run codeandconfirm'. Codex (Astra) reviews the diff and drives the real iOS + Android apps; this skill runs the coordinator, reads the structured verdict, and runs the bounded repair loop (max 5 cycles)."
---

# CodeAndConfirm — request QA, consume the verdict, repair, repeat

You (Claude) build. CodeAndConfirm launches an independent Codex QA worker that reviews and tests
the exact commit you hand it, then a gate validates the evidence. You never certify your own work.

## When

- Before `gh pr create` on a repo that has `codeandconfirm.toml`.
- Before pushing new commits to an open PR (a new head SHA invalidates the previous PASS).
- When the user says "run QA", "codeandconfirm", "get a verdict", or asks whether the branch is safe.

## How

1. **Commit first.** Only committed changes are tested. Run `git status --porcelain`; if dirty, commit or stash.
2. **Write acceptance criteria** (3–8 bullets: what the change must do, what must not regress). Save to a temp
   file and pass `--criteria-file`. Do not skip this; the QA worker derives scenarios from it.
3. **Run** (from the repo root; long-running, so use `run_in_background: true` and let its exit re-invoke you):
   ```bash
   codeandconfirm review --branch "$(git rev-parse --abbrev-ref HEAD)" --base main --criteria-file /tmp/criteria.md
   # existing PR:  codeandconfirm review --pr <url|number> [--publish]
   ```
   Print the `run-id` line to the user immediately. Exit codes: 0 PASS · 1 FAIL · 3 BLOCKED · 4 CANCELLED.
4. **Read the verdict:** `codeandconfirm report <run-id>` (Markdown) or `--json`. The `Gate checks` table and
   `Findings` section are authoritative. Screenshots and logs live in the run directory printed at the end.
5. **Act on the verdict:**
   - **PASS** → you may open/update the PR. Put the two-line summary from the report top in the PR body
     (`CodeAndConfirm PASS — run <id> — candidate <sha> vs base <sha>`), not the whole report.
   - **FAIL** → fix the product code for each blocking finding (they include reproduction steps and evidence).
     If the worker proposed a regression test patch (`patches/` in the run dir), review it and adopt it as a
     separate commit only if it is correct. Commit, then go to step 3. **Do not** weaken tests, delete assertions,
     lower `block_severity`, or drop journeys/suites from `codeandconfirm.toml` to get a PASS.
   - **BLOCKED** → infrastructure problem (device, backend ports, model, missing suite results). Read the
     `Why not PASS` bullets; fix the environment (`codeandconfirm doctor`) and `codeandconfirm resume <run-id>`
     or re-run. Never treat BLOCKED as PASS.
6. **Repair loop is bounded.** The coordinator counts repair cycles per branch durably; after 5 FAIL→fix cycles it
   refuses with BLOCKED and asks for a human. Stop earlier if a blocker is outside your control and say so.

## Rules

- Never run `codex`, CodeRabbit, or any other reviewer yourself for this purpose; CodeAndConfirm launches the
  QA worker with the correct role. Never post `@coderabbitai …` commands.
- Never merge. Never `--publish` on someone else's PR unless the user asked.
- Quote findings by title and severity; do not paraphrase evidence you did not open.
- Check `codeandconfirm gate-check --sha HEAD` before `gh pr create`; the pre-PR hook does the same and blocks
  if there is no current PASS for HEAD.
- If the report shows `model-verified` failing, tell the user: the run did not use the configured model.
