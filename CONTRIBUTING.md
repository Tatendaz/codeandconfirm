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
