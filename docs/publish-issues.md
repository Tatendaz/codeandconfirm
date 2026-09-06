# Publishing findings as issues (opt-in)

`codeandconfirm publish-issues <run-id>` files each finding of a finished run as a GitHub issue. Nothing is
published unless you run it; a review run never creates issues by itself.

```bash
codeandconfirm publish-issues 20260906-003402-cf79b7 --dry-run            # print what would be filed
codeandconfirm publish-issues 20260906-003402-cf79b7 --min-severity medium --label bug
codeandconfirm publish-issues <run-id> --repo-slug owner/repo             # branch runs without a PR
```

One issue per finding: `[<severity>] <platform>: <title>`, with severity/platform/area, the description,
numbered reproduction steps, the evidence file names and their local run directory, the base comparison
and the suggested regression test. The body names the run and the candidate/base SHAs; it carries no tool
banner beyond "Found by CodeAndConfirm hands-on QA".

Idempotent by construction:

- a hidden marker `<!-- codeandconfirm-finding:<key> -->` identifies the finding (platform + normalised
  title, independent of the run id); an issue anywhere in the repository that already carries the marker is
  reused, not duplicated;
- `runs/<run-id>/published-issues.json` records what this run filed or reused, so re-running the command is safe.

Exit codes: 0 when every finding was created, reused or skipped; 1 when a creation failed; 2 for a missing
report, an unknown repository, or `gh` not authenticated.
