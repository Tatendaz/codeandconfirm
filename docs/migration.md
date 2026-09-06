# Migrating from a bot-comment review loop

Many teams run a loop where a review bot comments on every PR, an agent watches for comments, fixes,
re-triggers the bot, and so on until the bot is quiet. CodeAndConfirm replaces the *orchestration* of that
loop with one gated, evidence-backed verdict per candidate. It does not replace CI, human review, or a
real-backend nightly. This page is a template: inventory what you have, then cut over in reversible steps.

## 1. Inventory

Fill this in for your setup. The first integration host had one of each row.

| Piece | Typical location | Role in the old loop | What to do |
|---|---|---|---|
| Global git `pre-push` hook running the bot's CLI | `core.hooksPath` dir | blocks pushes on bot findings; fails open | disable (keep the file) |
| Agent skill that opens PRs and *watches* bot comments | your Claude/Codex skills dir | the loop itself: poll → fix → re-trigger | replace the watcher + trigger sections with a pointer to the `codeandconfirm` skill; keep tests/coverage/docs gates |
| Rate-limit ledger for bot triggers | agent state dir | bookkeeping for triggers | obsolete once triggers stop |
| Global agent instructions mentioning the bot | `~/.claude/CLAUDE.md`, `$CODEX_HOME/AGENTS.md` | pushes every session into the protocol | **remove the section**: it leaks into the Codex QA worker (verified) |
| Repo instructions | `CLAUDE.md` / `AGENTS.md` | worktree isolation, review context | keep |
| CI workflows (unit/build/emulator, docs gate) | `.github/workflows/` | fast per-push checks | keep |
| Real-backend nightly | `.github/workflows/` | catches what emulators cannot | keep (CodeAndConfirm reports "local emulator" explicitly) |
| Branch protection | repo settings | human approval | add the CodeAndConfirm status as a required check |

`codeandconfirm doctor` warns while `$CODEX_HOME/AGENTS.md` still mentions the bot or a review loop.

## 2. Validate before switching

Run CodeAndConfirm on this host until you have seen, on real branches: a PASS with evidence, a FAIL with a
reproducible finding, and a BLOCKED you agree with. Keep the old loop running meanwhile.

## 3. Cut over (each step reversible)

1. **Remove the bot section from global agent instructions** (`~/.claude/CLAUDE.md`, `$CODEX_HOME/AGENTS.md`).
   Undo: restore the backup you made first.
2. **Retire the watcher** in the PR skill: replace "post-PR loop", "re-trigger review" and "rate limits" sections
   with: "Before `gh pr create` or pushing to a PR, run `codeandconfirm review …` and require PASS (see the
   `codeandconfirm` skill)". Keep local test/coverage/docs gates. Undo: restore the skill file.
3. **Disable the bot's git hook** without deleting it (its own config switch, e.g. `git config --global <bot>.prepush false`).
   Undo: unset the switch.
4. **Install the CodeAndConfirm pre-PR hook** (`hooks/pre-pr-gate.sh`) as a Claude `PreToolUse` hook on `Bash`.
   It only reads the approval record. Undo: remove the hook entry.
5. **Add the server-side backstop**: copy `examples/github/codeandconfirm-required.yml` into the repo and require
   its check in branch protection; publish statuses with `codeandconfirm review --pr <n> --publish`.
   Undo: remove the required check and the file.
6. **Leave the bot installed but quiet** (PR descriptions already carry its ignore marker, or pause it in its
   settings). Uninstall later once the team is comfortable. Undo: re-enable it.
7. **Keep** CI, the docs gate, the nightly real-backend job, and the human approval rule.

## 4. Anti-recursion guarantees after cut-over

- The QA worker role forbids launching `codex`, `claude`, any review bot, or bot commands, and forbids PR/issue
  writes; the gate fails a run whose checkout changed.
- The worker's instructions supersede inherited instruction files; `doctor` keeps warning until the leak is removed.
- Wording and formatting nits are not blocking findings: `block_severity` defaults to `high`, so re-runs are
  triggered by correctness, security, data-integrity, regression, or measured performance findings only.
- The repair loop is bounded (`max_repair_cycles`, durable per branch) — no infinite fix/re-review cycles.
