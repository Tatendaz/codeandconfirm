# Sample report

A real report from the first integration target, with local paths and account names generalized.

---

# CodeAndConfirm report — ✅ PASS

- **Run:** `20260906-003402-cf79b7`  ·  started 2026-09-06T00:34:02+00:00  ·  repair cycle 0
- **Candidate:** `55db8f9a23a9` (refs/pull/250/head) tested as **head**
- **Base:** `e5eaa0667d39` (main) · merge-base `e5eaa0667d39`
- **PR:** [OWNER/REPO#250](https://github.com/OWNER/REPO/pull/250) by AUTHOR · fork=False · trust=trusted
- **QA model:** requested `gpt-6-astra` · verified from session rollouts: `gpt-6-astra` · codex cli 0.153.4
- **Host/toolchain:** macOS 26.5.1 arm64 · Xcode 26.6 Build version · backend: firebase-emulator ports {'auth': 9099, 'firestore': 8080, 'storage': 9199, 'functions': 5001}
- **Devices:** ios: `62F7C035-C36A-45FB-82E7-E2614F8C6AB0` CodeAndConfirm iPhone CodeAndConfirm iPhone state=Booted · android: `emulator-5580` codeandconfirm_api34 Android 14

## Acceptance criteria

## Verification

Re-dispatched `nightly-real-backend.yml` on this branch (`workflow_dispatch`) — [OWNER/REPO#109](https://github.com/OWNER/REPO/actions/runs/33961302384) passed all four stages on head `ae01d5f` (iOS smoke, Android smoke, backend round-trip, cleanup). Local XCUITest runs aren't available in the guard environment.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01UTWvMPHBcRqNQ6XxT5ii3B

<!-- This is an auto-generated comment: release notes by coderabbit.ai -->

## Gate checks

| Check | Result | Detail |
|---|---|---|
| `candidate.sha-pinned` | ✅ | start=55db8f9a23 end=55db8f9a23 expected=55db8f9a23 |
| `candidate.unmodified` | ✅ | checkout has no tracked changes |
| `build.ios` | ✅ | exit=0 |
| `build.android` | ✅ | exit=0 |
| `install.ios.identity` | ✅ | installed build hash matches built artifact |
| `adapter.ios.proof` | ✅ | 14 steps |
| `install.android.identity` | ✅ | installed build hash matches built artifact |
| `adapter.android.proof` | ✅ | 14 steps |
| `suite.ios-ui-smoke` | ✅ | 2/2 passed |
| `suite.android-ui-smoke` | ✅ | 2/2 passed |
| `suite.mock-server-unit` | ✅ | 34/34 passed |
| `worker.review.completed` | ✅ | 17 commands, 0 tool calls |
| `worker.review.model-verified` | ✅ | gpt-6-astra (cli 0.153.4) |
| `worker.review.verdict` | ✅ | worker verdict PASS |
| `worker.ios.completed` | ✅ | 366 commands, 0 tool calls |
| `worker.ios.model-verified` | ✅ | gpt-6-astra (cli 0.153.4) |
| `worker.ios.verdict` (advisory) | ⚠️ | worker said FAIL, but its findings are below block_severity=high and no required journey failed; reported, not blocking: [low] Sign-in error persists when switching to signup |
| `worker.android.completed` | ✅ | 50 commands, 0 tool calls |
| `worker.android.model-verified` | ✅ | gpt-6-astra (cli 0.153.4) |
| `worker.android.verdict` (advisory) | ⚠️ | worker said FAIL, but its findings are below block_severity=high and no required journey failed; reported, not blocking: [medium] Double-tapping Create opens a different existing tree |
| `evidence.ios.hands-on` | ✅ | 123 interactions (min 8), 49 screenshots |
| `evidence.ios.build-identity` | ✅ | assigned build verified on device |
| `evidence.android.hands-on` | ✅ | 96 interactions (min 8), 39 screenshots |
| `evidence.android.build-identity` | ✅ | assigned build verified on device |
| `journey.auth.signup.ios` | ✅ | passed with 5 evidence file(s) |
| `journey.auth.signup.android` | ✅ | passed with 5 evidence file(s) |
| `journey.tree.create-once.ios` | ✅ | passed with 5 evidence file(s) |
| `journey.tree.create-once.android` | ✅ | passed with 5 evidence file(s) |
| `journey.person.add.ios` | ✅ | passed with 8 evidence file(s) |
| `journey.person.add.android` | ✅ | passed with 8 evidence file(s) |
| `journey.nav.tree-back.ios` | ✅ | passed with 6 evidence file(s) |
| `journey.nav.tree-back.android` | ✅ | passed with 7 evidence file(s) |
| `journey.app.restart-persistence.ios` | ✅ | passed with 4 evidence file(s) |
| `journey.app.restart-persistence.android` | ✅ | passed with 5 evidence file(s) |
| `findings.blocking` | ✅ | no findings at/above high |

## Native suites (run by the coordinator)

| Suite | Exit | Tests | Passed | Failed | Skipped | Duration | Log |
|---|---|---|---|---|---|---|---|
| ios-ui-smoke | 0 | 2 | 2 | 0 | 0 | 1m44s | `suite-ios-ui-smoke.log` |
| android-ui-smoke | 0 | 2 | 2 | 0 | 0 | 44s | `suite-android-ui-smoke.log` |
| mock-server-unit | 0 | 34 | 34 | 0 | 0 | 0s | `suite-mock-server-unit.log` |

## Builds

- **ios:** ok exit=0 in 37s · artifact `App.app` · sha256 `ce51de665b17762d` · version 1.0
- **android:** ok exit=0 in 40s · artifact `app-debug.apk` · sha256 `8a5d4d8a6f5bae20` · version 0.1.0

## QA workers

### review — PASS

exit=0 timed_out=False commands=17 tool_calls=0 tokens in/out=618736/6373 thread=`01a07427-3194-7930-b905-bfa978760194`

Reviewed all three changed files and traced tree creation, lookup, caching, authorization, and cleanup; no introduced defect found. Independently ran 125 Functions unit tests and Swift suites covering 112 cache assertions, 17 in-flight guard assertions, and 26 validation assertions, all passing. The prepared backend is a LOCAL Firebase emulator, not the real cloud backend. Coordinator results confirm two passing iOS tests, but neither exercises the changed test03; no device was assigned to this reviewer. All tracked files remained byte-identical to their starting state, including the pre-existing emulator configuration modification.

Untested / limitations:
- Changed test03 UI execution, repeated simulator runs, and accessibility behavior: no device assigned to this worker.
- Real-cloud nightly run: only the supplied historical claim was available; coordinator tests used the local emulator.
- Firestore rules suite: it clears the shared demo-project database before each test, which could disrupt other workers.
- Device build SHA256 verification: no assigned device; candidate git SHA was verified instead.

### ios — FAIL

exit=0 timed_out=False commands=366 tool_calls=0 tokens in/out=4836015/12083 thread=`01a07427-3194-78c2-ab40-92b51c60e447`

All five required journeys and the manual unique-tree-name smoke flow passed against the LOCAL Firebase emulator. Build identity matched before and after testing, and backend checks confirmed one record per tree, persisted edits, correct relationships, and deletion. The coordinator’s result bundle confirms its two iOS tests passed; I did not rerun them. One low-severity UX defect was reproduced: a failed sign-in error remains visible after switching to signup, which accounts for the FAIL verdict. This is not established as a candidate regression; the diff changes tests and documentation only. Simulator accessibility recovery succeeded, and no crashes were observed in the collected journey logs.

Untested / limitations:
- Real cloud backend and the referenced nightly run; testing used the local Firebase emulator.
- Exact changed test03 execution and repeated cross-account cache carryover; the coordinator suite covered two other iOS tests.
- Software-keyboard occlusion, rotation, offline behavior, VoiceOver operation, and Dynamic Type.
- Sustained loading-state visuals and base-build comparison.

### android — FAIL

exit=0 timed_out=False commands=50 tool_calls=0 tokens in/out=5843379/12692 thread=`01a07427-3194-7492-838d-ba7dfa4d9fc6`

Completed all five required Android journeys against the LOCAL Firebase emulator, not the real cloud backend. The APK hash and foreground package matched the assigned build at the start and end. Signup, sign-in, unique record creation, person editing/deletion, navigation, and restart persistence passed. Rapid double-tapping Create reproducibly opened an older tree through the closing dialog, although it created only one backend record. This Android defect is not attributed to the candidate’s iOS-test-only change; no base build was provided. The inspected coordinator Android suite log reports two passing tests, and collected app logs contain no fatal crash.

Untested / limitations:
- Changed iOS XCUITest execution and real-backend nightly behavior: outside this Android worker’s assigned device/backend.
- Base comparison: no base Android build provisioned.
- Offline mode, rotation, large text, and TalkBack traversal were not exercised.
- Transient loading was observed in accessibility output but not visually captured.

## Scenarios

| Worker | Id | Status | Evidence | Notes |
|---|---|---|---|---|
| review | review.correctness | passed | `review-correctness.txt`, `validators.txt` | No issue found in code review. The 19-character name fits the 80-character limit. Changed XCUITest was not executed. |
| review | review.security | passed | `review-security.txt`, `functions-unit.txt` | No introduced security issue. Existing cache-isolation behavior remains unchanged; live rules enforcement was not tested. |
| review | review.data-integrity | passed | `review-data-integrity.txt`, `offline-sync.txt`, `inflight-guard.txt`, `functions-unit.txt` | All executed assertions passed. The generated label does not change record IDs or fix existing stale-cache behavior. |
| review | review.deployment | passed | `review-deployment.txt`, `identity-and-scope.txt`, `coordinator-ios-result-summary.txt`, `coordinator-suite-ios-ui-smoke.txt` | No coordinated rules, functions, indexes, or configuration deployment is required. The iOS test target must be rebuilt. Historical cloud verification was not in |
| review | review.parity | passed | `review-parity.txt`, `coordinator-suite-android-ui-smoke.txt` | No introduced product parity issue. Android retains its existing fixed-name selector; no Android interaction was performed by this reviewer. |
| review | review.performance | passed | `review-performance.txt` | Only bounded string generation is added per test invocation. No runtime profiling performed. |
| ios | auth.signup | passed | `032-signup-recovered.png`, `034-profile.png`, `036-signed-out.png`, `043-signed-back-in.png` | One accessibility recovery succeeded after the system password prompt obscured the accessibility tree. |
| ios | tree.create-once | passed | `055-first-tree-created.png`, `059-double-tap-two-trees.png`, `backend-trees-two.json`, `backend-final-verification.txt` | The first tree remained empty; all subsequent people were stored in the second tree. |
| ios | person.add | passed | `096-person-ready-save.png`, `100-ada-detail.png`, `113-ada-edited.png`, `backend-ada-edited.json` | The birth switch required a coordinate tap on its visible thumb after accessibility trailing-edge taps did not toggle it. |
| ios | nav.tree-back | passed | `115-tree-after-edit.png`, `117-trees-back.png`, `121-tree-view.png`, `123-timeline-view.png` | Toolbar navigation worked. In Tree view, the adapter’s back swipe panned the canvas, so the My trees toolbar button was used. |
| ios | app.restart-persistence | passed | `129-restart-trees.png`, `131-restart-person-persists.png`, `restart-logs.txt`, `build-identity-final.json` | Final installed and foreground build SHA-256 matched the assigned build. |
| ios | diff.unique-tree-identity | passed | `067-second-tree-empty.png`, `160-generations-list.png`, `162-generations-tree.png`, `backend-three-people.json` | Manual end-to-end coverage of the changed lookup behavior. The exact test03 XCUITest and nightly cross-account retry/cache conditions were not executed. |
| ios | states.empty-loading-error | failed | `067-second-tree-empty.png`, `069-add-person-form.png`, `185-wrong-password.png`, `189-stale-signin-error-on-signup.png` | Empty states and wrong-password feedback were readable. Error-state reset failed; a sustained loading state was not captured. |
| ios | ui.keyboard-dialogs | skipped | `050-whitespace-tree-disabled.png`, `136-delete-confirmation.png`, `qa-environment-notes.txt` | Partial coverage only: the software keyboard was not displayed, so keyboard occlusion was not verified. |
| android | auth.signup | passed | `027-my-trees-fresh.png`, `032-signed-out.png`, `039-wrong-password.png`, `068-signin-success.png` | The password-edit retry appended text; successful sign-in was independently confirmed from a fresh form. |
| android | tree.create-once | passed | `082-first-tree-created.png`, `086-before-double-create.png`, `090-two-trees-after-double.png`, `backend-verification.txt` | Record uniqueness passed; unintended navigation during double-tap is reported separately. |
| android | person.add | passed | `108-person-created.png`, `124-edited-detail-reopened.png`, `145-delete-cancelled.png`, `148-person-removed.png` | The edit updated the existing person record. |
| android | nav.tree-back | passed | `124-edited-detail-reopened.png`, `126-nav-back-one.png`, `128-nav-back-two.png`, `131-tree-view.png` | The edited person and 1984 birth year remained consistent across views. |
| android | app.restart-persistence | passed | `138-restart-trees.png`, `141-restart-person-detail.png`, `restart-logs.txt`, `identity-start.txt` | Verified before the subsequent intentional person deletion. |
| android | diff.unique-tree-identity | passed | `090-two-trees-after-double.png`, `092-unique-tree-opened.png`, `108-person-created.png`, `backend-person-created.json` | Android analogue only; this does not execute the changed iOS XCUITest. |
| android | diff.create-dialog-tap-through | failed | `086-before-double-create.png`, `088-after-double-create.png`, `153-repeat-before-double.png`, `155-repeat-after-double.png` | Reproduced twice; no duplicate backend records. |
| android | ui.keyboard-dialogs | passed | `023-signup-keyboard.png`, `073-whitespace-tree.png`, `080-create-first-keyboard.png`, `145-delete-cancelled.png` | Signup/Create actions were visible; whitespace submission was rejected. Accessibility labels supported the exercised interactions. |

## Findings

### [MEDIUM] Double-tapping Create opens a different existing tree  (ux, android, from android)

With the keyboard open, the first Create tap closes the dialog and the second tap reaches an underlying tree row. Creating cf79b7a2 and later cf79b7a3 both opened cf79b7a1, placing the user in the wrong tree for subsequent edits. Each new tree still had exactly one backend record. The candidate contains no Android product changes.

Reproduction:
1. 1. Sign up on the assigned Android build and leave the getting-started card visible.
2. 2. Create Smoke Tree cf79b7a1 normally.
3. 3. Open New tree, enter Smoke Tree cf79b7a2, and leave the keyboard open.
4. 4. Run ccdevice android tap Create --exact --times 2 --interval-ms 80.
5. 5. Observe the app open Smoke Tree cf79b7a1 instead of remaining on My trees.
6. 6. Return to My trees and repeat with cf79b7a3; the older tree opens again.

Evidence: `086-before-double-create.png`, `088-after-double-create.png`, `153-repeat-before-double.png`, `155-repeat-after-double.png`, `157-final-three-trees.png`, `backend-final-trees.json`, `rapid-create-reproduction.txt`, `actions.jsonl`

Compared to base: not compared

Suggested regression test: Add a device-level test with an existing tree and the keyboard open. Deliver two physical taps at Create 80 ms apart; assert My trees remains visible, no existing tree opens, and exactly one new backend record exists.

### [LOW] Sign-in error persists when switching to signup  (ux, ios, from ios)

After an incorrect-password attempt, switching to signup retains “Incorrect email or password.” before any signup submission. The message describes the previous operation and is misleading on the new form. Repeated switching reproduced the stale state.

Reproduction:
1. 1. Open Sign in with the provided test account email.
2. 2. Enter Incorrect-1234 and submit.
3. 3. Confirm “Incorrect email or password.” appears.
4. 4. Tap “Need an account? Sign up”.
5. 5. Observe the same error under the signup fields without submitting signup.

Evidence: `185-wrong-password.png`, `189-stale-signin-error-on-signup.png`, `actions.jsonl`

Compared to base: not compared

Suggested regression test: After a failed sign-in, switch authentication modes and assert that the previous operation’s error is cleared.

## Artifacts

- Run directory: `$CODEANDCONFIRM_HOME/runs/20260906-003402-cf79b7`
- Evidence: `$CODEANDCONFIRM_HOME/runs/20260906-003402-cf79b7/evidence` · Codex workspaces: `$CODEANDCONFIRM_HOME/runs/20260906-003402-cf79b7/codex` · command log: `$CODEANDCONFIRM_HOME/runs/20260906-003402-cf79b7/artifacts/commands.jsonl`

_Generated by CodeAndConfirm, an independent community project (not affiliated with Anthropic or OpenAI)._
