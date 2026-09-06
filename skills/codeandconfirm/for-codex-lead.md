# CodeAndConfirm for a Codex lead (AGENTS.md snippet)

Paste this into the repository's `AGENTS.md` (or `~/.codex/AGENTS.md`) when **Codex implements** and the
CodeAndConfirm QA worker (Claude, `[roles] qa = "claude"`) reviews and tests. It is the Codex counterpart of
`SKILL.md`; the rules are the same.

```markdown
## Independent QA before any pull request (CodeAndConfirm)

You build; you never certify your own work. Before `gh pr create`, and before pushing new commits to an
open PR:

1. Commit. Only committed changes are tested (`git status --porcelain` must be empty).
2. Write acceptance criteria (3–8 bullets) to a temp file.
3. Run from the repo root and wait for it (it is long-running):
   codeandconfirm review --branch "$(git rev-parse --abbrev-ref HEAD)" --base main --criteria-file <file>
   Exit codes: 0 PASS · 1 FAIL · 3 BLOCKED · 4 CANCELLED. Print the run-id line to the user.
4. Read `codeandconfirm report <run-id>`. The gate table and Findings are authoritative.
5. PASS → open/update the PR with the report's two-line summary. FAIL → fix every blocking finding
   (they carry reproduction steps and evidence), commit, go to 3; never weaken tests, lower
   `block_severity` or drop journeys/suites to get a PASS. BLOCKED → fix the environment
   (`codeandconfirm doctor`), then `codeandconfirm resume <run-id>`; BLOCKED is never PASS.
6. The repair loop is bounded (5 cycles per branch); after that a human decides.

Never launch another reviewer yourself (`claude`, `codex`, CodeRabbit, `@coderabbitai …`); the coordinator
launches the QA worker with the correct role. Never merge. `codeandconfirm gate-check --sha HEAD` must
pass before `gh pr create`.
```
