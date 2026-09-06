# Server-side check (merge backstop)

Local hooks are advisory: `git push --no-verify`, a different terminal, or a bypass variable gets around them.
The durable enforcement point is branch protection on the server.

1. Copy `examples/github/codeandconfirm-required.yml` from this repository into the target repository.
2. Run reviews for PRs with `codeandconfirm review --pr <n> --publish`. On completion the coordinator posts:
   - a PR comment containing the report (truncated to 60 kB; screenshots stay local), and
   - a **commit status** on the tested head SHA with context `codeandconfirm/qa` and state
     `success` (PASS) / `failure` (FAIL) / `error` (BLOCKED, CANCELLED).
3. In branch protection / rulesets, require the check **"CodeAndConfirm verdict present for head"**. The
   workflow looks up the status on `pull_request.head.sha` and fails when it is not `success`.

Because a status is bound to a SHA, any new commit on the PR has no status until a new run publishes one —
a previous PASS cannot carry over. When the run tested the **merge candidate** (`--merge-candidate`), the
status is still posted on the head SHA; the report body says which ref was tested.

## What this proves, and does not

The status is published with the developer's `gh` token from the machine that ran QA. It proves that a run
completed for that SHA and that the local gate accepted the evidence. It does not prove the machine was
trustworthy or that the configuration was not weakened. Keep the human review requirement, review
`codeandconfirm.toml` changes like code, and consider a dedicated QA machine or CI runner for publishing
statuses on protected branches.
