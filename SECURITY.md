# Security

## Reporting

Please report vulnerabilities privately to the maintainers (see the repository's security advisory page once
public) rather than opening a public issue. Include a reproduction and the version (`codeandconfirm --version`).

## Model

CodeAndConfirm executes the candidate's build and test commands and an unsandboxed AI worker on the host for
trusted candidates. Its security depends on the trust decisions described in
[docs/trust-boundaries.md](docs/trust-boundaries.md). In short:

- Only run device QA on code you would run yourself. Fork PRs are static-review-only by default.
- Approval records are local files, not attestations. Use the server-side status check for merge decisions.
- Reports and logs are redacted by pattern; review them before sharing outside your machine.
- XML from devices and test runners is parsed with entity expansion refused.

## Scope

In scope: the coordinator, gate, device adapters, prompts, hooks and workflows in this repository.
Out of scope: vulnerabilities in Codex, Claude Code, Xcode, Android tooling, or the application under test.
