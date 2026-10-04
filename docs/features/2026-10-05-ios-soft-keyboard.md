# Feature: iOS software keyboard and the hidden Save Password sheet

**Branch:** feat/ios-soft-keyboard
**Date:** 2026-10-05

QA runs on the iOS simulator could not see two classes of bugs, and one system sheet blocked them
outright.

1. **No on-screen keyboard.** `ccdevice ios type` used `idb ui text`, which sends hardware-key
   events. iOS then sets `HardwareKeyboardLastSeen` and `AutomaticMinimizationEnabled` in
   `com.apple.keyboard.preferences` and minimizes the software keyboard for every app launched
   afterwards. A button covered by the keyboard on a phone looked fine in QA.
2. **A tree that "collapsed" after sign-up and sign-in.** With password AutoFill on, iOS shows a
   "Save Password?" sheet after every credential submit. The sheet runs outside the app, so
   `idb ui describe-all` returned only the Application node and `describe-point` failed with
   "No translation object returned". Closing the sheet brought the full tree back at once.
   Restarting backboardd only appeared to help because it closed the sheet.

## What changed

- `ccdevice ios type` pastes: it copies the text to the simulator pasteboard (`simctl pbcopy`),
  taps the trailing edge of the focused field (the element with the `IsEditing` trait) until the
  edit menu shows Paste, taps it, and checks the text landed (secure fields: bullet count). It
  prints `typed (paste)`. If no focused field or Paste item appears it falls back to HID typing
  and prints a WARNING plus `typed (hid-keys)`. `CAC_IOS_TYPE=keys` forces HID typing.
- `ccdevice ios key return|delete|space` taps the keyboard's own key when the keyboard is up.
- Every `IOSSimulator.launch()` writes both keyboard preferences back to false, which re-arms the
  software keyboard for that launch (no reboot needed). `ccdevice ios arm-keyboard` does it on
  demand and relaunches the app.
- `keyboard_shown()` works on iOS: the keyboard's keys are in the tree with the `KeyboardKey` trait.
- `ccdevice <platform> kbd "<label>"`: is a control under the keyboard? Exit 0 clear, 3 covered,
  2 no keyboard, 4 not in the tree. Android reads the IME frame from `dumpsys window InputMethod`.
- `ccdevice ios disable-autofill` and the coordinator turn off Settings > General > AutoFill &
  Passwords > AutoFill Passwords and Passkeys once per simulator (stored on disk, verified from the
  ManagedConfiguration user settings). Opt out with `[platforms.ios] disable_password_autofill = false`.
- The coordinator arms the keyboard when it reserves the simulator and again before the post-suite
  relaunch (the adapter proof and XCUITest typing are HID events). `[platforms.ios] software_keyboard
  = false` restores the old behaviour.
- `ccdevice ios tree` and the idb error name the out-of-process-sheet cause when only the
  Application node is visible. The QA worker notes explain paste typing, `arm-keyboard`, `kbd`
  and how to deal with such sheets.
- The adapter proof never picks a keyboard key as its navigation target (it tapped Dictate once),
  and iOS `dismiss_keyboard()` checks the result and records `shown_after` honestly.

## Notes

- Verified on an iOS 26.5 simulator: one `idb ui text` dropped the keyboard from 36 keys to 0 and
  flipped both preferences to 1; paste typing kept 36 keys and both preferences at 0. A fresh
  sign-up with AutoFill on reproduced the one-node tree; with AutoFill off the same sign-in kept
  the full tree. `disable-autofill` worked on a simulator it had never touched (35 s once, then
  "already off").
- Known limit: iOS has no system-wide keyboard dismissal, so `dismiss-keyboard` can report
  `shown_after: true` on screens without a tap-to-dismiss area.
- Settings navigation uses English labels; on another locale `disable-autofill` fails softly and
  the run report carries a note.
