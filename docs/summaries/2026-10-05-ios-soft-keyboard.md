# Session: iOS software keyboard and the hidden Save Password sheet

**Branch:** feat/ios-soft-keyboard
**Date:** 2026-10-05

## Prompts

1. "Make CodeAndConfirm's iOS QA keep the software keyboard visible (and survive the
   accessibility-tree collapse), then rerun QA on [my app's] PR and merge it if clean."
2. "Continue." (after an overnight pause)

## Work

Read the blocked run's iOS worker log: the keyboard helper found no keyboard, and the tree
dropped to the Application node after the notification prompt and again after sign-in.
Reproduced both on the simulator. One `idb ui text` minimized the keyboard and set both keyboard
preferences; rewriting them and relaunching the app restored it. A screenshot of the "collapsed"
state showed a Save Password sheet; tapping Not Now restored the tree, and turning AutoFill off
stopped the sheet. Ported a paste-based typing path, keyboard detection, `kbd`, `arm-keyboard`
and `disable-autofill` into the adapter and the coordinator. Fourteen new device-free tests use
recorded idb shapes. Ran the adapter proof and paste/kbd checks on a real simulator.

## Decisions

- Paste through the edit menu rather than tapping keyboard keys one by one: one menu tap per
  field, no shift or symbol planes, and the text can be checked in the field afterwards.
- HID typing stays as a loud fallback, so a field without the `IsEditing` trait still gets text.
- AutoFill is turned off through the Settings UI and checked in the settings file. Editing the
  five plist copies offline would depend on undocumented file layouts.
- No forced reboot: rewriting the preferences before each launch is enough.
