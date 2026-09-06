# Sample report

**PASS under the configured policy, with two non-blocking findings.**

This is an edited excerpt of a real report from run `20260906-003402-cf79b7`.
The selected results and findings are preserved. Local paths, account identifiers,
device identifiers, session links, and unrelated PR-description history are omitted.
It is not a verbatim export or a claim that the app has no bugs.

## Tested candidate

| Field | Recorded value |
|---|---|
| Started | 2026-09-06 at 00:34:02 UTC |
| Candidate | `55db8f9a23a9`, PR head |
| Base | `e5eaa0667d39` |
| Backend | Local Firebase emulator, not the real cloud backend |
| QA model | `gpt-6-astra`, verified from worker session rollouts |
| Codex CLI | 0.153.4 |
| Blocking threshold | `high` |

The candidate changed an iOS UI-test tree lookup and documentation. The review
included required signup, tree creation, person editing, navigation, and restart
journeys. The exact changed XCUITest was not executed; manual device coverage is
not a substitute for that missing test execution.

## Selected gate checks

| Check | Result | Detail |
|---|---|---|
| Candidate integrity | PASS | Pinned SHA, no tracked changes |
| iOS and Android builds | PASS | Installed hashes matched built artifacts |
| iOS native smoke suite | PASS | 2/2 tests |
| Android native smoke suite | PASS | 2/2 tests |
| Mock-server suite | PASS | 34/34 tests |
| Required device journeys | PASS | All five on each platform, with evidence |
| Worker model verification | PASS | Requested model verified for all three workers |
| Blocking findings | PASS | No finding at or above `high` |
| iOS worker verdict | Advisory FAIL | Low-severity stale authentication error |
| Android worker verdict | Advisory FAIL | Medium-severity wrong-tree navigation |

The device workers reported FAIL because they found defects. The gate accepted
the run because neither finding met `block_severity = "high"` and no required
journey failed. Lower-severity findings remain in the report for human review.
Changing the threshold to `medium` would make the Android finding blocking.

## Findings

### Medium: double-tapping Create opens a different existing tree

On Android, the first tap closes the create dialog. The second reaches an older
tree row underneath it, opening the wrong tree. Each new tree still had exactly
one backend record.

1. Sign up and leave the getting-started card visible.
2. Create a first tree normally.
3. Open New tree and enter a different name with the keyboard open.
4. Tap Create twice, 80 ms apart.
5. Observe the app open the older tree. Repeat with another new name.

The worker reproduced this twice. No base build was compared, so the report does
not establish it as a regression introduced by this candidate.

Evidence filenames in the original Android run directory:
`086-before-double-create.png`, `088-after-double-create.png`,
`153-repeat-before-double.png`, `155-repeat-after-double.png`,
`backend-final-trees.json`, and `actions.jsonl`.

Suggested regression test: assert that rapid Create taps leave the user on My
trees, open no older tree, and create exactly one backend record.

### Low: sign-in error persists when switching to signup

On iOS, switching to signup after a failed sign-in leaves "Incorrect email or
password." visible before any signup submission.

1. Submit an incorrect password on Sign in.
2. Confirm the inline error appears.
3. Tap "Need an account? Sign up".
4. Observe the old error on the signup form without submitting it.

No base build was compared. This finding is not established as a candidate regression.

Evidence filenames in the original iOS run directory:
`185-wrong-password.png`, `189-stale-signin-error-on-signup.png`, and `actions.jsonl`.

Suggested regression test: switching authentication modes clears the previous
operation's error.

## Coverage limits

- The changed iOS `test03` and repeated cross-account cache carryover were not tested.
- Real-cloud nightly behavior was not verified in this local-emulator run.
- No base-build comparison established the origin of either UX finding.
- Rotation, large-text behavior, VoiceOver/TalkBack traversal, and controlled
  performance measurements were not covered.
- iOS software-keyboard occlusion and sustained loading visuals were not verified.

## Where the evidence lives

The operator's full report and artifacts remain under
`$CODEANDCONFIRM_HOME/runs/20260906-003402-cf79b7/`. The filenames above describe
that local archive; they are not downloadable attachments in this repository.

For screenshots included in this repository, see the separate
[duplicate-record demo](demo.md). That demo tests an intentionally broken branch
and returns FAIL; it is not the run summarized on this page.
