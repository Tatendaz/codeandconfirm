"""Config, scheduler reservations, result parsers, candidate resolution, redaction."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from codeandconfirm import candidate as cand_mod
from codeandconfirm.config import Config, render, EXAMPLE_CONFIG, REPO_CONFIG_NAME, LOCAL_CONFIG_NAME
from codeandconfirm.results import parse_junit_files, parse_results
from codeandconfirm.scheduler import Reservation, release_all_for, list_locks, auto_functional_slots
from codeandconfirm.util import redact, run, CmdResult


# --- config --------------------------------------------------------------------------

def test_config_layering(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(home))
    (home / "config.toml").write_text('[codex]\nbinary = "/usr/bin/false"\n[scheduler]\nmax_functional_runs = 3\n')
    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    (repo / REPO_CONFIG_NAME).write_text(EXAMPLE_CONFIG)
    (repo / LOCAL_CONFIG_NAME).write_text('[codex]\nreasoning_effort = "low"\n[profiles.pr.codex]\nreasoning_effort = "minimal"\n')
    cfg = Config.load(repo_root=repo)
    assert cfg.get("codex.binary") == "/usr/bin/false"
    assert cfg.get("scheduler.max_functional_runs") == 3
    # the example config selects profile `pr` by default; profiles merge over the layered result, and the
    # local file can still override a profile's values through its own [profiles.pr.*] table
    assert cfg.get("profile.name") == "pr"
    assert cfg.get("codex.reasoning_effort") == "minimal"
    assert Config.load(repo_root=repo, profile="full").get("codex.reasoning_effort") == "high"
    assert cfg.get("codex.model") == "gpt-6-astra"
    assert set(cfg.platforms) == {"ios", "android"}
    assert len(cfg.sources) == 3


def test_example_config_has_no_personal_paths():
    for needle in ("/Users/", "/home/", "@gmail", "@me.com"):
        assert needle not in EXAMPLE_CONFIG


def test_render_placeholders():
    assert render("x {run_dir}/y {auth_port}", {"run_dir": "/r", "auth_port": 9}) == "x /r/y 9"
    with pytest.raises(KeyError):
        render("{nope}", {})
    assert render("{nope}", {}, strict=False) == "{nope}"


# --- reservations -----------------------------------------------------------------------

def test_reservation_conflict_and_stale_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path))
    a = Reservation("ios-device:X", "run-a", ttl_s=60)
    assert a.try_acquire()
    b = Reservation("ios-device:X", "run-b", ttl_s=60)
    assert not b.try_acquire()                       # held by a live owner
    # simulate a crashed owner: dead pid
    info = a.current(); info.owner_pid = 999999
    from codeandconfirm.util import write_json
    write_json(a.path, info.__dict__)
    assert b.try_acquire()                           # stale lock reclaimed
    assert b.current().owner_run == "run-b"
    b.release()
    assert b.current() is None


def test_release_all_for(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path))
    Reservation("r1", "run-z", 60).try_acquire(); Reservation("r2", "run-z", 60).try_acquire(); Reservation("r3", "other", 60).try_acquire()
    assert sorted(release_all_for("run-z")) == ["r1", "r2"]
    assert [l.resource for l in list_locks()] == ["r3"]


def test_auto_slots():
    assert auto_functional_slots(16) == 1
    assert auto_functional_slots(48) == 2
    assert auto_functional_slots(128) == 5


# --- results ------------------------------------------------------------------------------

def test_junit_parse(tmp_path):
    x = tmp_path / "TEST-a.xml"
    x.write_text('<testsuite name="s" tests="3"><testcase classname="C" name="ok"/><testcase classname="C" name="bad"><failure message="x"/></testcase>'
                 '<testcase classname="C" name="skip"><skipped/></testcase></testsuite>')
    r = parse_junit_files([x])
    assert (r.total, r.passed, r.failed, r.skipped) == (3, 1, 1, 1)
    assert r.failed_tests == ["C.bad"] and not r.green


def test_junit_node_reporter_shape(tmp_path):
    x = tmp_path / "node.xml"
    x.write_text('<?xml version="1.0"?><testsuites><testcase name="a" classname="t"/><testcase name="b" classname="t"><failure/></testcase></testsuites>')
    r = parse_junit_files([x])
    assert (r.total, r.passed, r.failed) == (2, 1, 1) and r.parsed


def test_junit_refuses_dtd(tmp_path):
    x = tmp_path / "evil.xml"
    x.write_text('<!DOCTYPE t [<!ENTITY e "x">]><testsuite><testcase name="&e;"/></testsuite>')
    r = parse_junit_files([x])
    assert not r.parsed


def test_missing_xcresult_not_parsed(tmp_path):
    r = parse_results("xcresult", tmp_path / "nope.xcresult")
    assert not r.parsed and not r.green


# --- candidate ----------------------------------------------------------------------------

def _git(repo: Path, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("1\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feat")
    (repo / "a.txt").write_text("2\n"); (repo / "b.txt").write_text("x\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "feat")
    return repo


def test_resolve_branch_and_worktree(tmp_path):
    repo = make_repo(tmp_path)
    c = cand_mod.resolve_branch(repo, "feat", "main")
    assert c.source == "branch" and sorted(c.changed_files) == ["a.txt", "b.txt"]
    assert c.base_sha == c.merge_base_sha != c.candidate_sha
    wt = cand_mod.create_worktree(repo, c.candidate_sha, tmp_path / "wt")
    assert cand_mod.head_sha(wt) == c.candidate_sha
    assert cand_mod.worktree_is_pristine(wt) == (True, [])
    (wt / "a.txt").write_text("tampered\n")
    ok, offending = cand_mod.worktree_is_pristine(wt)
    assert not ok and offending == ["a.txt"]
    # the developer's checkout is untouched
    assert (repo / "a.txt").read_text() == "2\n"
    cand_mod.remove_worktree(repo, wt)
    assert not wt.exists()


def test_uncommitted_changes_are_flagged(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("dirty\n")
    c = cand_mod.resolve_branch(repo, "feat", "main")
    assert "uncommitted" in c.acceptance_criteria


def test_parse_pr_specs():
    assert cand_mod.parse_pr("https://github.com/o/r/pull/12") == ("o/r", 12)
    assert cand_mod.parse_pr("o/r#7") == ("o/r", 7)
    assert cand_mod.parse_pr("9", "o/r") == ("o/r", 9)
    with pytest.raises(ValueError):
        cand_mod.parse_pr("9")


# --- util ------------------------------------------------------------------------------------

def test_redaction():
    s = "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and Bearer abcdefghijklmnopqrstuvwxyz123 and sk-abcdefghijklmnopqrstuv"
    r = redact(s)
    assert "ghp_" not in r and "abcdefghijklmnopqrstuvwxyz123" not in r and "sk-abc" not in r


def test_run_preserves_exit_code_and_timeout():
    r = run("exit 7")
    assert r.exit_code == 7 and not r.ok
    r = run("sleep 5", timeout=0.5)
    assert r.timed_out and not r.ok
    r = run("false | true")   # pipefail-free shell: exit 0; the coordinator records real per-suite codes separately
    assert isinstance(r, CmdResult)


# --- suites -----------------------------------------------------------------------------

def test_exit_code_only_suite_is_green_without_a_retry(tmp_path):
    from codeandconfirm.target import run_suite
    out = run_suite("logic", {"command": "echo ok", "results": {"kind": "none"}}, checkout=tmp_path, values={}, artifacts_dir=tmp_path,
                    timeout_s=30, record_to=tmp_path / "commands.jsonl", retries=1)
    assert out.green and out.attempts == 1 and out.flaky_history == []
    bad = run_suite("logic", {"command": "exit 3", "results": {"kind": "none"}}, checkout=tmp_path, values={}, artifacts_dir=tmp_path,
                    timeout_s=30, record_to=tmp_path / "commands.jsonl", retries=1)
    assert not bad.green and bad.exit_code == 3


# --- iOS software keyboard, paste typing, AutoFill sheet (recorded idb output, no device) ----------------------

from codeandconfirm.device import ios_idb  # noqa: E402
from codeandconfirm.device.android_adb import parse_ime_top  # noqa: E402
from codeandconfirm.device.base import Element  # noqa: E402
from codeandconfirm.device.cli import keyboard_cover  # noqa: E402


def _ax(type_, label=None, value=None, traits=(), x=0, y=0, w=10, h=10, uid=None):
    return {"type": type_, "AXLabel": label, "AXValue": value, "AXUniqueId": uid, "traits": list(traits),
            "frame": {"x": x, "y": y, "width": w, "height": h}, "enabled": True, "pid": 1}


# Shapes recorded from an iOS 26.5 simulator: a sign-up form with the Email field focused and the keyboard up.
SIGNUP_KEYBOARD_UP = [
    _ax("Application", "Demo", traits=["None"], w=402, h=874),
    _ax("TextField", None, "Email", ["Scrollable", "TextEntry", "TextOperationsAvailable", "IsEditing"], 117, 244.7, 195, 23.3),
    _ax("TextField", None, "Password", ["Scrollable", "TextEntry", "TextOperationsAvailable", "SecureTextField"], 117, 301.3, 195, 22),
    _ax("Button", "Create account", None, ["Button", "Scrollable"], 77, 347.3, 248, 50),
    _ax("Button", "Have an account? Sign in", None, ["Button", "Scrollable"], 130, 650, 142, 14),
    _ax("Button", "q", None, ["KeyboardKey"], 4, 640, 36, 42),
    _ax("Button", "return", None, ["KeyboardKey"], 300, 800, 90, 42),
]


def _res(code=0, stdout="", stderr=""):
    return CmdResult(argv=[], cwd=None, exit_code=code, stdout=stdout, stderr=stderr, started_at="", duration_s=0)


def _fake_sim(monkeypatch, trees, *, pbcopy_ok=True):
    """An IOSSimulator whose tree() replays `trees` (the last one repeats) and that records taps/idb calls."""
    monkeypatch.setattr(ios_idb.time, "sleep", lambda s: None)
    calls = {"taps": [], "idb": [], "simctl": [], "pbcopy": []}

    def fake_run(argv, **kw):
        if argv[:3] == ["xcrun", "simctl", "pbcopy"]:
            calls["pbcopy"].append(kw.get("input_text"))
            return _res(0 if pbcopy_ok else 1)
        raise AssertionError(f"unexpected run {argv}")
    monkeypatch.setattr(ios_idb, "run", fake_run)

    class Fake(ios_idb.IOSSimulator):
        def __init__(self):
            super().__init__("UDID-TEST")
            self._trees = list(trees)

        def tree(self):
            raw = self._trees.pop(0) if len(self._trees) > 1 else self._trees[0]
            return ios_idb.parse_describe_all(raw)

        def tap_xy(self, x, y):
            calls["taps"].append((x, y))

        def _idb_cmd(self, *args, **kw):
            calls["idb"].append(args)

        def _simctl(self, *args, **kw):
            calls["simctl"].append(args)
            return _res(0, "0")
    return Fake(), calls


def test_ios_keyboard_detection_from_tree():
    els = ios_idb.parse_describe_all(SIGNUP_KEYBOARD_UP)
    assert len(ios_idb.keyboard_keys(els)) == 2
    assert ios_idb.keyboard_top(els) == 640 - ios_idb.KEYBOARD_ACCESSORY_PT
    assert ios_idb.focused_field(els).value == "Email"
    assert ios_idb.soft_key(els, "return").label == "return" and ios_idb.soft_key(els, "delete") is None
    no_kb = ios_idb.parse_describe_all(SIGNUP_KEYBOARD_UP[:5])
    assert ios_idb.keyboard_top(no_kb) is None and ios_idb.keyboard_keys(no_kb) == []


def test_ios_text_landed_plain_and_secure():
    plain = Element(0, "TextField", value="ada@example.test", extra={"traits": ["TextEntry"]})
    assert ios_idb.text_landed(plain, "ada@example.test") and not ios_idb.text_landed(plain, "bob")
    secure = Element(0, "TextField", value="•" * 12, extra={"traits": ["SecureTextField"]})
    assert ios_idb.text_landed(secure, "CacTest-1234", before="Password")   # placeholder is not bullets
    assert not ios_idb.text_landed(secure, "CacTest-1234-x", before="")
    assert not ios_idb.text_landed(Element(0, "TextField", value="Password", extra={"traits": ["SecureTextField"]}), "abc")


def test_ios_type_pastes_and_keeps_keyboard(monkeypatch):
    monkeypatch.delenv("CAC_IOS_TYPE", raising=False)
    menu = SIGNUP_KEYBOARD_UP + [_ax("StaticText", "Paste", None, ["MenuItem", "TextOperationsAvailable"], 45, 283, 70, 44)]
    after = [dict(e) for e in SIGNUP_KEYBOARD_UP]
    after[1] = dict(after[1], AXValue="ada@example.test")
    # read field, 1st tap: no menu, 2nd tap: menu, verify
    dev, calls = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP, SIGNUP_KEYBOARD_UP, menu, after])
    dev.type_text("ada@example.test")
    assert dev.last_type_method == "paste" and dev.last_type_warning is None
    assert calls["pbcopy"] == ["ada@example.test"]
    assert not any(a[:2] == ("ui", "text") for a in calls["idb"]), "no HID typing"
    assert calls["taps"][-1] == (80, 305)                       # the Paste item's center
    assert calls["taps"][0][0] >= 117 + 195 - 8                  # field tapped at its trailing edge


def test_ios_type_falls_back_loudly_without_a_focused_field(monkeypatch):
    monkeypatch.delenv("CAC_IOS_TYPE", raising=False)
    unfocused = [dict(e, traits=[t for t in e["traits"] if t != "IsEditing"]) for e in SIGNUP_KEYBOARD_UP]
    dev, calls = _fake_sim(monkeypatch, [unfocused])
    dev.type_text("hello")
    assert dev.last_type_method == "hid-keys"
    assert "arm-keyboard" in dev.last_type_warning and "IsEditing" in dev.last_type_warning
    assert ("ui", "text", "hello") in calls["idb"]


def test_ios_type_falls_back_when_paste_never_lands(monkeypatch):
    monkeypatch.delenv("CAC_IOS_TYPE", raising=False)
    dev, calls = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP])     # the Paste menu never appears
    dev.type_text("hello")
    assert dev.last_type_method == "hid-keys" and "no Paste item" in dev.last_type_warning
    assert len(calls["taps"]) == 4


def test_ios_type_keys_mode_is_opt_in(monkeypatch):
    monkeypatch.setenv("CAC_IOS_TYPE", "keys")
    dev, calls = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP])
    dev.type_text("x")
    assert dev.last_type_method == "hid-keys" and calls["pbcopy"] == []


def test_ios_return_key_taps_the_on_screen_key(monkeypatch):
    dev, calls = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP])
    dev.key("return")
    assert calls["taps"] == [(345, 821)] and calls["idb"] == []
    dev2, calls2 = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP[:5]])   # no keyboard: HID fallback
    dev2.key("return")
    assert calls2["idb"] == [("ui", "key", "40")]


def test_ios_launch_rearms_keyboard_unless_disabled(monkeypatch):
    dev, calls = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP])
    monkeypatch.delenv("CAC_IOS_SOFT_KEYBOARD", raising=False)
    dev.launch("com.example.app")
    writes = [a for a in calls["simctl"] if a[:3] == ("spawn", "UDID-TEST", "defaults")]
    assert {a[5] for a in writes} == set(ios_idb.KEYBOARD_PREF_KEYS) and all(a[-1] == "false" for a in writes)
    monkeypatch.setenv("CAC_IOS_SOFT_KEYBOARD", "0")
    off, calls_off = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP])
    off.launch("com.example.app")
    assert not any(a[0] == "spawn" for a in calls_off["simctl"])


def test_autofill_setting_read_from_plist(tmp_path):
    import plistlib
    p = tmp_path / "UserSettings.plist"
    assert ios_idb.autofill_enabled_from_plist(p) is None                        # unreadable
    p.write_bytes(plistlib.dumps({"restrictedBool": {}}))
    assert ios_idb.autofill_enabled_from_plist(p) is True                        # factory default
    p.write_bytes(plistlib.dumps({"restrictedBool": {"allowPasswordAutoFill": {"ask": False, "value": False}}}))
    assert ios_idb.autofill_enabled_from_plist(p) is False


def test_keyboard_cover_verdicts():
    class Dev:
        platform = "ios"

        def find(self, needle, exact=False, tree=None):
            return next((e for e in tree if e.matches(needle, exact=exact)), None)
    els = ios_idb.parse_describe_all(SIGNUP_KEYBOARD_UP)
    assert keyboard_cover(Dev(), "Create account", els=els)[0] == 0
    rc, msg = keyboard_cover(Dev(), "Have an account? Sign in", els=els)
    assert rc == 3 and msg.startswith("COVERED")
    assert keyboard_cover(Dev(), "Nope", els=els)[0] == 4
    assert keyboard_cover(Dev(), "Create account", els=ios_idb.parse_describe_all(SIGNUP_KEYBOARD_UP[:5]))[0] == 2
    android = Dev(); android.platform = "android"
    btn = [Element(0, "Button", label="Save", x=0, y=1700, w=200, h=60)]
    assert keyboard_cover(android, "Save", els=btn, android_top=1517)[0] == 3
    assert keyboard_cover(android, "Save", els=btn, android_top=1800)[0] == 0


def test_parse_ime_top():
    out = "  Window #3 Window{abc u0 InputMethod}:\n    mFrame=... frame=[0,1170][1080,2400]\n    mGivenContentInsets=[0,347][0,0]\n"
    assert parse_ime_top(out) == 1517.0
    assert parse_ime_top("frame=[0,1170][1080,2400]") == 1170.0
    assert parse_ime_top("nothing here") is None


def test_collapsed_tree_error_names_the_out_of_process_sheet(monkeypatch):
    sim = ios_idb.IOSSimulator("UDID-TEST")
    monkeypatch.setattr(sim, "_idb_cmd", lambda *a, **k: _res(1, stderr="No translation object returned for simulator. This means you have likely specified a point"))
    with pytest.raises(ios_idb.DeviceError, match="Save Password"):
        sim.tree()


def test_ios_dismiss_keyboard_checks_the_result(monkeypatch):
    hidden = SIGNUP_KEYBOARD_UP[:5]
    neutral = _ax("StaticText", "or continue with", None, ["StaticText"], 163, 409, 75, 12)
    # first read (keyboard up, with a neutral label above it), then keyboard_shown() sees it gone
    dev, calls = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP + [neutral], hidden])
    dev.dismiss_keyboard()
    assert calls["taps"] == [(200, 415)]
    stuck, calls2 = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP + [neutral]])   # never goes away: second, different tap
    stuck.dismiss_keyboard()
    assert len(calls2["taps"]) == 2 and calls2["taps"][1][1] == 60


def test_prepare_simulator_arms_keyboard_and_notes_autofill_failure():
    from types import SimpleNamespace
    from codeandconfirm.coordinator import Coordinator

    class Dev:
        def __init__(self, autofill):
            self.calls, self._autofill = [], autofill

        def write_keyboard_prefs(self):
            self.calls.append("prefs")

        def keyboard_prefs_armed(self):
            return True

        def disable_password_autofill(self):
            self.calls.append("autofill")
            return self._autofill
    me = SimpleNamespace(ctx={}, log=lambda m: None)
    ok = Dev({"ok": True, "changed": True, "detail": "AutoFillToggle=0"})
    Coordinator._prepare_simulator(me, ok, {})
    assert ok.calls == ["prefs", "autofill"] and me.ctx["notes"] == []
    bad = Dev({"ok": False, "changed": False, "detail": "no AutoFillToggle in Settings"})
    Coordinator._prepare_simulator(me, bad, {})
    assert any("Save Password" in n for n in me.ctx["notes"])
    off = Dev({"ok": True, "changed": False, "detail": ""})
    Coordinator._prepare_simulator(SimpleNamespace(ctx={}, log=lambda m: None), off,
                                   {"software_keyboard": False, "disable_password_autofill": False})
    assert off.calls == []


def test_ios_type_never_retypes_after_paste_was_tapped(monkeypatch):
    monkeypatch.delenv("CAC_IOS_TYPE", raising=False)
    menu = SIGNUP_KEYBOARD_UP + [_ax("StaticText", "Paste", None, ["MenuItem"], 45, 283, 70, 44)]
    reformatted = [dict(e) for e in SIGNUP_KEYBOARD_UP]
    reformatted[1] = dict(reformatted[1], AXValue="(555) 010-0000")      # field reformatted the pasted value
    dev, calls = _fake_sim(monkeypatch, [SIGNUP_KEYBOARD_UP, menu, reformatted])
    dev.type_text("5550100000")
    assert dev.last_type_method == "paste-unverified" and "not retyped" in dev.last_type_warning
    assert not any(a[:2] == ("ui", "text") for a in calls["idb"])


def test_ios_text_landed_needs_a_change_for_plain_fields():
    same = Element(0, "TextField", value="ada", extra={"traits": ["TextEntry"]})
    assert not ios_idb.text_landed(same, "ada", before="ada")          # a paste that did nothing
    assert ios_idb.text_landed(Element(0, "TextField", value="adaada", extra={"traits": []}), "ada", before="ada")


def test_ios_soft_keyboard_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("CAC_IOS_SOFT_KEYBOARD", "1")
    assert ios_idb.IOSSimulator("U", soft_keyboard=False).soft_keyboard is False
    monkeypatch.setenv("CAC_IOS_SOFT_KEYBOARD", "0")
    assert ios_idb.IOSSimulator("U").soft_keyboard is False and ios_idb.IOSSimulator("U", soft_keyboard=True).soft_keyboard
