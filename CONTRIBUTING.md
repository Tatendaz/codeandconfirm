# Contributing

Thanks for helping make independent QA boring and reliable.

## Ground rules

- Keep the runtime dependency-free (Python standard library). Dev dependencies live under `[project.optional-dependencies].dev`.
- Never add personal paths, account names, device UDIDs, project ids or tokens to tracked files. `tests/test_core.py::test_example_config_has_no_personal_paths` guards the example config; extend it if you add examples.
- The gate must stay conservative: a change that lets a narrative or an exit code alone produce PASS will not be merged.
- Every new gate rule needs a test in `tests/test_gate.py` (one positive, one negative).
- Device adapters must keep the contract in [docs/adapters.md](docs/adapters.md) and pass `ccdevice <platform> proof` on a real device before a PR.

## Development

```bash
uv venv .venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

Integration checks (need Xcode/Android tooling and a Codex login):

```bash
codeandconfirm doctor --config examples/firebase-two-app.toml --repo /path/to/that/repo
ccdevice ios proof; ccdevice android proof
```

## Pull requests

- One topic per PR; explain what evidence you looked at.
- Run a real `codeandconfirm review` on your branch where practical and attach the report's `Gate checks` table.
- No merge without a review from a maintainer; do not merge your own PRs.

## Publishing a sample report

`scripts/export-sample-report.py` makes limited string substitutions and overwrites
`docs/sample-report.md`. It does not fully redact identifiers or remove session
links and unrelated PR history. Run it only in an isolated worktree, keep the draft
local, and review it before any commit, push, or upload.

1. Remove account emails/IDs, device IDs, private paths, session links, credentials,
   backend document IDs, and unrelated PR-description history from the entire draft.
2. Preserve the real verdict, candidate/base provenance, findings, and coverage
   limits. Label excerpts and omissions; do not imply a fresh run.
3. Review all text and link targets in the diff and rendered report. Search for
   token/key patterns, email addresses, UUIDs, `/Users/`, `/home/`, session URLs,
   and private backend names. Pattern searches alone are not a privacy guarantee.
4. Inspect any images separately. Do not assume text redaction alters screenshots.
5. Have a reviewer confirm the publication copy contains only intended public data.
   Do not commit the raw report, source transcripts, or unreviewed attachments.
