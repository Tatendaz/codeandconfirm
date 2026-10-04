"""iOS Simulator adapter: `xcrun simctl` for lifecycle, `idb` for accessibility + input.

idb (https://fbidb.io) is device-scoped: it talks to one simulator by UDID and never
touches the desktop, so several simulators can be driven concurrently.
Fallbacks: screenshots via `simctl io`, coordinate taps when accessibility lookup fails.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import time
from pathlib import Path

from ..util import run, sha256_file, which
from .base import Device, DeviceError, Element, Evidence

HID_KEYS = {"return": 40, "enter": 40, "delete": 42, "backspace": 42, "tab": 43, "space": 44, "escape": 41}

# Software-keyboard keys a named key maps to. Tapping these keeps the on-screen keyboard; a HID key event does not.
SOFT_KEYS = {"return": ("return", "done", "go", "next", "search", "send", "join", "route", "continue"),
             "enter": ("return", "done", "go", "next", "search", "send", "join", "route", "continue"),
             "delete": ("delete",), "backspace": ("delete",), "space": ("space",)}

# Any HID keyboard event (`idb ui text`, `idb ui key`) makes iOS record a hardware keyboard and minimize the
# software keyboard for every app launched afterwards, so keyboard-covers-the-button bugs become invisible.
# Writing these two preferences back to false re-arms the software keyboard for the NEXT app launch.
KEYBOARD_PREFS_DOMAIN = "com.apple.keyboard.preferences"
KEYBOARD_PREF_KEYS = ("AutomaticMinimizationEnabled", "HardwareKeyboardLastSeen")
# Height of the suggestion / AutoFill bar that sits above the key rows (points).
KEYBOARD_ACCESSORY_PT = 48
# Where iOS keeps the "AutoFill Passwords and Passkeys" setting (ManagedConfiguration user settings).
AUTOFILL_SETTINGS_PLIST = ("Containers/Shared/SystemGroup/systemgroup.com.apple.configurationprofiles/"
                           "Library/ConfigurationProfiles/UserSettings.plist")
COLLAPSED_TREE_HINT = ("the accessibility tree shows only the Application node. Usually a system sheet that runs "
                       "outside the app is on screen (Save Password?, Sign in with Apple, a share sheet): its "
                       "contents are invisible to idb. Take a screenshot; tap its button with --xy (points = "
                       "pixels / `ccdevice ios scale`), or run `ccdevice ios recover-accessibility`, which closes it.")


def parse_describe_all(raw: list[dict]) -> list[Element]:
    """`idb ui describe-all` JSON → Elements (frames in points)."""
    els = []
    for i, e in enumerate(raw):
        f = e.get("frame") or {}
        els.append(Element(
            index=i, type=e.get("type") or (e.get("role") or "").replace("AX", ""),
            label=(e.get("AXLabel") or "").strip(), value=(e.get("AXValue") or "") if isinstance(e.get("AXValue"), str) else str(e.get("AXValue") or ""),
            identifier=e.get("AXUniqueId") or "", enabled=bool(e.get("enabled", True)),
            x=float(f.get("x", 0)), y=float(f.get("y", 0)), w=float(f.get("width", 0)), h=float(f.get("height", 0)),
            extra={"pid": e.get("pid"), "traits": e.get("traits"), "custom_actions": e.get("custom_actions")},
        ))
    return els


def _traits(e: Element) -> list:
    return list((e.extra or {}).get("traits") or [])


def keyboard_keys(els: list[Element]) -> list[Element]:
    """Software-keyboard keys in a tree. They are only present while the keyboard is shown in full."""
    return [e for e in els if "KeyboardKey" in _traits(e)]


def keyboard_top(els: list[Element]) -> float | None:
    """Top edge (points) of the software keyboard including its suggestion bar, or None when it is not shown."""
    keys = keyboard_keys(els)
    return (min(e.y for e in keys) - KEYBOARD_ACCESSORY_PT) if keys else None


def focused_field(els: list[Element]) -> Element | None:
    """The text field that has the caret (iOS marks it with the IsEditing trait)."""
    return next((e for e in els if "IsEditing" in _traits(e)), None)


def paste_item(els: list[Element]) -> Element | None:
    """The Paste entry of the text edit menu."""
    return next((e for e in els if "MenuItem" in _traits(e) and e.label == "Paste"), None)


def is_secure(e: Element) -> bool:
    return "SecureTextField" in _traits(e) or e.type == "SecureTextField"


def text_landed(field: Element, text: str, before: str = "") -> bool:
    """Did `text` reach the field? A secure field shows one bullet per character, so count the bullets."""
    if is_secure(field):
        v = field.value or ""
        bullets = len(v) if v and set(v) <= {"•", "●", "*"} else 0
        prior = len(before) if before and set(before) <= {"•", "●", "*"} else 0
        return bullets - prior >= len(text)
    value = field.value or ""
    return text in value and value != before


def soft_key(els: list[Element], name: str) -> Element | None:
    labels = SOFT_KEYS.get(name.lower(), ())
    for want in labels:
        for e in keyboard_keys(els):
            if e.label.strip().lower() == want:
                return e
    return None


def autofill_enabled_from_plist(path: Path) -> bool | None:
    """True/False from the ManagedConfiguration user settings; None when the file is unreadable.
    A missing key means the factory default, which is enabled."""
    try:
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    entry = ((data.get("restrictedBool") or {}).get("allowPasswordAutoFill") or {})
    return bool(entry.get("value", True))


class IOSSimulator(Device):
    platform = "ios"

    def __init__(self, udid: str, evidence: Evidence | None = None, timeout: float = 60,
                 soft_keyboard: bool | None = None):
        super().__init__(udid, evidence)
        self.udid = udid
        self.timeout = timeout
        # None = decide from CAC_IOS_SOFT_KEYBOARD (how the coordinator tells ccdevice in a worker shell).
        self.soft_keyboard = (os.environ.get("CAC_IOS_SOFT_KEYBOARD", "1") != "0") if soft_keyboard is None else soft_keyboard
        self._idb = which("idb")
        self.last_type_method: str | None = None
        self.last_type_warning: str | None = None

    # --- low level ------------------------------------------------------------
    def _simctl(self, *args: str, timeout: float | None = None, check: bool = True):
        r = run(["xcrun", "simctl", *args], timeout=timeout or self.timeout)
        if check and not r.ok:
            raise DeviceError(f"simctl {' '.join(args[:2])} failed (exit {r.exit_code}): {r.stderr.strip()[:400]}")
        return r

    def _idb_cmd(self, *args: str, timeout: float | None = None, check: bool = True):
        if not self._idb:
            raise DeviceError("idb not installed (brew install idb-companion; uv tool install fb-idb)")
        r = run([self._idb, *args, "--udid", self.udid], timeout=timeout or self.timeout)
        if check and not r.ok:
            raise DeviceError(f"idb {' '.join(args[:2])} failed (exit {r.exit_code}): {(r.stderr or r.stdout).strip()[:400]}")
        return r

    def available(self) -> tuple[bool, str]:
        r = run(["xcrun", "simctl", "list", "devices", "-j"], timeout=30)
        if not r.ok:
            return False, "simctl unavailable"
        for devs in json.loads(r.stdout)["devices"].values():
            for d in devs:
                if d["udid"] == self.udid:
                    return d.get("state") == "Booted", f"{d['name']} state={d.get('state')}"
        return False, "udid not found"

    # --- primitives -----------------------------------------------------------
    def tree(self) -> list[Element]:
        # Right after a launch/restart the snapshot can be empty for a moment; retry briefly.
        deadline = time.monotonic() + 6.0
        while True:
            r = self._idb_cmd("ui", "describe-all", timeout=90, check=False)
            if not r.ok:
                msg = (r.stderr or r.stdout).strip()[:400]
                if "No translation object" in msg:
                    raise DeviceError(f"idb describe-all failed: {msg}. Likely cause: {COLLAPSED_TREE_HINT}")
                raise DeviceError(f"idb ui describe-all failed (exit {r.exit_code}): {msg}")
            try:
                raw = json.loads(r.stdout)
            except json.JSONDecodeError as e:
                raise DeviceError(f"idb describe-all returned non-JSON: {r.stdout[:200]!r}") from e
            if len(raw) > 1 or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        return parse_describe_all(raw)

    def screenshot(self, path: Path) -> Path:
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        r = run(["xcrun", "simctl", "io", self.udid, "screenshot", "--type=png", str(path)], timeout=30)
        if not r.ok or not path.exists():
            self._idb_cmd("screenshot", str(path), timeout=30)
        return path

    def tap_xy(self, x: int, y: int) -> None:
        self._idb_cmd("ui", "tap", str(x), str(y))

    def type_text(self, text: str) -> None:
        """Enter text into the focused field through the pasteboard and the Paste menu, so the software keyboard
        stays on screen. Typing through idb sends HID key events, and iOS then minimizes the software keyboard
        for every later app launch. Falls back to HID typing (loudly) when no focused field or Paste item can be
        found; set CAC_IOS_TYPE=keys to force HID typing."""
        time.sleep(0.6)   # let the keyboard and caret settle after the focusing tap
        self.last_type_method, self.last_type_warning = None, None
        why = "CAC_IOS_TYPE=keys"
        if os.environ.get("CAC_IOS_TYPE", "paste").lower() != "keys":
            status, why = self._paste(text)
            if status == "ok":
                self.last_type_method = "paste"
                return
            if status == "unverified":
                # Paste was tapped: typing again could enter the text twice. Report it and let the caller look.
                self.last_type_method = "paste-unverified"
                self.last_type_warning = f"{why}; not retyped (it may have landed reformatted): check the field"
                return
        self._idb_cmd("ui", "text", text)
        self.last_type_method = "hid-keys"
        self.last_type_warning = (f"typed with HID key events ({why}): iOS now minimizes the software keyboard, so "
                                  "keyboard-coverage checks are invalid until `ccdevice ios arm-keyboard`")

    def _paste(self, text: str, tries: int = 4) -> tuple[str, str]:
        """("ok" | "unverified" | "failed", reason). "failed" means nothing was entered, so HID typing is safe;
        "unverified" means Paste was tapped but the field does not show the text as expected."""
        if not text:
            return "ok", "empty text"
        els = self.tree()
        field = focused_field(els)
        if field is None:
            return "failed", "no focused text field (IsEditing) in the tree"
        before = field.value or ""
        r = run(["xcrun", "simctl", "pbcopy", self.udid], input_text=text, timeout=30)
        if not r.ok:
            return "failed", f"simctl pbcopy failed: {r.stderr.strip()[:200]}"
        # Tap near the trailing edge: the caret goes to the end of any existing text, and a tap on the caret of
        # a focused field toggles the edit menu (Paste, AutoFill...). It can take two taps to open it.
        tx, ty = int(field.x + field.w - min(8, field.w / 4)), field.center[1]
        for _ in range(tries):
            self.tap_xy(tx, ty)
            time.sleep(0.9)
            item = paste_item(self.tree())
            if item is None:
                continue
            self.tap_xy(*item.center)
            time.sleep(0.8)
            now = focused_field(self.tree())
            if now is not None and text_landed(now, text, before):
                return "ok", "pasted"
            return "unverified", "Paste was tapped but the field does not show the text"
        return "failed", f"no Paste item appeared after {tries} taps on the field"

    def key(self, name: str) -> None:
        n = name.lower()
        if n == "home":
            self._idb_cmd("ui", "button", "HOME"); return
        if n == "back":
            # iOS has no back key: prefer a navigation-bar back button, else an edge swipe.
            try:
                els = self.tree()
            except DeviceError:
                els = []
            back = next((e for e in els if e.type == "Button" and e.label.lower() in ("back", "‹ back") and e.y < 120), None)
            if back is None:
                back = next((e for e in els if e.type == "Button" and e.y < 120 and e.x < 60 and e.label), None)
            if back is not None:
                self.tap_xy(*back.center); return
            w, h = self.screen_size()
            self.swipe(2, h // 2, int(w * 0.75), h // 2, 250); return
        if n in SOFT_KEYS:
            try:
                k = soft_key(self.tree(), n)
            except DeviceError:
                k = None
            if k is not None:   # tap the on-screen key: a HID key event would minimize the keyboard
                self.tap_xy(*k.center); return
        code = HID_KEYS.get(n)
        if code is None:
            if n.isdigit():
                code = int(n)
            else:
                raise DeviceError(f"unknown key {name!r}; known: {sorted(HID_KEYS)} home back")
        self._idb_cmd("ui", "key", str(code))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._idb_cmd("ui", "swipe", str(x1), str(y1), str(x2), str(y2), "--duration", str(duration_ms / 1000))

    def launch(self, app: str, args: list[str] | None = None) -> dict:
        if self.soft_keyboard:
            self.write_keyboard_prefs()   # every launch starts with the software keyboard armed
        t0 = time.monotonic()
        r = self._simctl("launch", "--terminate-running-process", self.udid, app, *(args or []), timeout=60)
        pid = None
        if ":" in r.stdout:
            try:
                pid = int(r.stdout.strip().rsplit(":", 1)[1])
            except ValueError:
                pid = None
        launch_ms = int((time.monotonic() - t0) * 1000)
        info = {"pid": pid, "launch_call_ms": launch_ms, "args": args or []}
        self._rec("launch", app=app, **info)
        return info

    def terminate(self, app: str) -> None:
        self._simctl("terminate", self.udid, app, check=False)
        self._rec("terminate", app=app)

    def install(self, path: Path) -> dict:
        path = Path(path)
        if not path.exists():
            raise DeviceError(f"app bundle not found: {path}")
        self._simctl("install", self.udid, str(path), timeout=180)
        ident = app_identity(path)
        self._rec("install", app=ident.get("bundle_id"), **ident)
        return ident

    def uninstall(self, app: str) -> None:
        self._simctl("uninstall", self.udid, app, check=False)
        self._rec("uninstall", app=app)

    def clear_data(self, app: str) -> None:
        """Reset the app to a fresh-install state WITHOUT reinstalling: (re)installing an app has been
        observed to wedge the simulator's accessibility bridge. Terminates the app, wipes its data container
        (Documents, Library, tmp) and resets the simulator keychain (where Firebase-style sessions live)."""
        self.terminate(app)
        r = self._simctl("get_app_container", self.udid, app, "data", check=False)
        container = r.stdout.strip()
        if not container:
            raise DeviceError(f"{app} not installed")
        for sub in ("Documents", "Library", "tmp", "SystemData"):
            d = Path(container) / sub
            if d.exists():
                for child in d.iterdir():
                    run(["rm", "-rf", str(child)])
        self._simctl("keychain", self.udid, "reset", check=False)
        self._rec("clear-data", app=app, method="container-wipe+keychain-reset")

    def app_state(self, app: str) -> dict:
        r = self._simctl("get_app_container", self.udid, app, "app", check=False)
        container = r.stdout.strip() if r.ok else None
        state = {"installed": bool(container), "container": container, "foreground": None, "identity": None}
        if container:
            state["identity"] = app_identity(Path(container))
        try:
            els = self.tree()
            if els:
                top = els[0]
                pid = top.extra.get("pid")
                comm = run(["ps", "-p", str(pid), "-o", "comm="]).stdout.strip() if pid else ""
                state["foreground"] = {"label": top.label, "pid": pid, "executable": comm,
                                       "is_target": bool(container and comm.startswith(container))}
        except DeviceError as e:
            state["foreground_error"] = str(e)
        return state

    def logs(self, app: str, since: str = "2m", grep: str | None = None) -> str:
        r = self._simctl("get_app_container", self.udid, app, "app", check=False)
        proc = None
        if r.ok and r.stdout.strip():
            info = Path(r.stdout.strip()) / "Info.plist"
            if info.exists():
                with open(info, "rb") as fh:
                    proc = plistlib.load(fh).get("CFBundleExecutable")
        pred = f'process == "{proc}"' if proc else 'subsystem CONTAINS "com.apple"'
        if grep:
            pred += f' AND eventMessage CONTAINS[c] "{grep}"'
        out = run(["xcrun", "simctl", "spawn", self.udid, "log", "show", "--last", since, "--style", "compact", "--predicate", pred], timeout=120)
        return out.stdout[-20000:]

    def keyboard_shown(self) -> bool | None:
        """True when the full software keyboard is on screen (its keys are in the tree). A keyboard minimized
        because iOS saw hardware key events counts as not shown."""
        try:
            return bool(keyboard_keys(self.tree()))
        except DeviceError:
            return None

    def keyboard_top(self, els: list[Element] | None = None) -> float | None:
        return keyboard_top(els if els is not None else self.tree())

    # --- software keyboard + password AutoFill ------------------------------------------------
    def write_keyboard_prefs(self) -> None:
        for k in KEYBOARD_PREF_KEYS:
            self._simctl("spawn", self.udid, "defaults", "write", KEYBOARD_PREFS_DOMAIN, k, "-bool", "false", check=False)

    def keyboard_prefs_armed(self) -> bool:
        vals = [self._simctl("spawn", self.udid, "defaults", "read", KEYBOARD_PREFS_DOMAIN, k, check=False).stdout.strip()
                for k in KEYBOARD_PREF_KEYS]
        return all(v == "0" for v in vals)

    def arm_software_keyboard(self, app: str | None = None, launch_args: list[str] | None = None,
                              reboot: bool = False) -> dict:
        """Re-arm the software keyboard. The preferences take effect for the next app launch, so the app is
        relaunched (its session survives, an unsaved form does not); `reboot` restarts the whole simulator."""
        self.write_keyboard_prefs()
        steps = ["keyboard prefs written"]
        if reboot:
            self._simctl("shutdown", self.udid, check=False, timeout=120)
            self._simctl("boot", self.udid, check=False, timeout=120)
            self._simctl("bootstatus", self.udid, "-b", check=False, timeout=240)
            steps.append("simulator rebooted")
        if app:
            self.terminate(app); time.sleep(1.0)
            self.launch(app, launch_args or []); time.sleep(2.0)
            steps.append(f"relaunched {app}")
        armed = self.keyboard_prefs_armed()
        self._rec("arm-keyboard", ok=armed, steps=steps)
        return {"ok": armed, "steps": steps}

    def _data_dir(self) -> Path:
        r = self._simctl("getenv", self.udid, "HOME", check=False)
        home = r.stdout.strip()
        return Path(home) if r.ok and home else Path.home() / "Library/Developer/CoreSimulator/Devices" / self.udid / "data"

    def password_autofill_enabled(self) -> bool | None:
        return autofill_enabled_from_plist(self._data_dir() / AUTOFILL_SETTINGS_PLIST)

    def disable_password_autofill(self) -> dict:
        """Turn off Settings > General > AutoFill & Passwords > AutoFill Passwords and Passkeys. With it on, every
        sign-up or sign-in raises a "Save Password?" sheet that runs outside the app: idb then sees only the
        Application node until the sheet is closed. The setting is stored on disk, so this runs once per
        simulator. It drives the Settings app (English labels, the toggle's id is AutoFillToggle)."""
        if self.password_autofill_enabled() is False:
            return {"ok": True, "changed": False, "detail": "already off"}
        settings = "com.apple.Preferences"
        try:
            self.launch(settings); time.sleep(2.5)
            for label in ("General", "AutoFill & Passwords"):
                el = self.wait_for(label, 10, exact=True, type_="Button")
                if el is None:
                    return {"ok": False, "changed": False, "detail": f"Settings has no {label!r} button"}
                self.tap_xy(*el.center); time.sleep(1.5)
            tog = None
            for _ in range(10):
                tog = self.find("AutoFillToggle", exact=True, field_="id", tree=self.tree())
                if tog is not None:
                    break
                time.sleep(0.5)
            if tog is None:
                return {"ok": False, "changed": False, "detail": "no AutoFillToggle in Settings"}
            changed = False
            if tog.value == "1":
                self.tap_xy(*self.tap_point(tog, "trailing")); time.sleep(2.0); changed = True
                tog = self.find("AutoFillToggle", exact=True, field_="id", tree=self.tree()) or tog
            self._simctl("spawn", self.udid, "defaults", "write", "com.apple.WebUI", "AutoFillPasswords", "-bool", "false", check=False)
            ok = tog.value == "0" or self.password_autofill_enabled() is False
            self._rec("disable-password-autofill", ok=ok, changed=changed)
            return {"ok": ok, "changed": changed, "detail": f"AutoFillToggle={tog.value}"}
        finally:
            self.terminate(settings)

    def dismiss_keyboard(self) -> None:
        """Resign the first responder by tapping a neutral static label above the keyboard, then the top of the
        screen; the keyboard's keys are in the tree, so the result is checked rather than assumed."""
        shown = None
        for attempt in range(2):
            try:
                els = self.tree()
            except DeviceError:
                els = []
            top = keyboard_top(els)
            if attempt and top is None:
                break
            neutral = next((e for e in els if e.type == "StaticText" and e.label and e.y > 40
                            and "KeyboardKey" not in _traits(e) and (top is None or e.y + e.h < top)), None)
            if neutral is not None and attempt == 0:
                self.tap_xy(*neutral.center)
            else:
                w, _ = self.screen_size()
                self.tap_xy(w // 2, 60)
            time.sleep(0.5)
            shown = self.keyboard_shown()
            if not shown:
                break
        self._rec("dismiss-keyboard", shown_after=shown)

    def screen_size(self) -> tuple[int, int]:
        try:
            els = self.tree()
            if els:
                return int(els[0].w), int(els[0].h)
        except DeviceError:
            pass
        return 393, 852

    def memory_kb(self, app: str) -> int | None:
        st = self.app_state(app)
        pid = (st.get("foreground") or {}).get("pid")
        if not pid:
            return None
        r = run(["ps", "-p", str(pid), "-o", "rss="])
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None

    def describe_point(self, x: int, y: int) -> dict:
        """What the accessibility server thinks sits at a point (helps make coordinate taps informed)."""
        r = self._idb_cmd("ui", "describe-point", str(x), str(y), "--json", check=False)
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"raw": (r.stdout or r.stderr).strip()[:300]}

    def bar_elements(self, rows: tuple[int, ...] = (66, 100), step: int = 28) -> list[Element]:
        """Discover navigation-bar / toolbar buttons that `describe-all` omits by hit-testing points along the
        bar rows. Deduplicated by frame; indices start at 1000 so they never collide with tree indices."""
        w, _ = self.screen_size()
        seen: dict[tuple, Element] = {}
        for y in rows:
            for x in range(16, w - 8, step):
                d = self.describe_point(x, y)
                f = d.get("frame") if isinstance(d, dict) else None
                if not f or not (d.get("AXLabel") or d.get("AXUniqueId")):
                    continue
                if f.get("y", 0) > 140:      # only bar-height elements
                    continue
                key = (round(f.get("x", 0)), round(f.get("y", 0)), round(f.get("width", 0)), round(f.get("height", 0)))
                if key in seen:
                    continue
                seen[key] = Element(index=1000 + len(seen), type=d.get("type") or "Button", label=(d.get("AXLabel") or "").strip(),
                                    value=str(d.get("AXValue") or ""), identifier=d.get("AXUniqueId") or "", enabled=bool(d.get("enabled", True)),
                                    x=float(f.get("x", 0)), y=float(f.get("y", 0)), w=float(f.get("width", 0)), h=float(f.get("height", 0)),
                                    extra={"source": "bar-probe", "traits": d.get("traits")})
        return list(seen.values())

    def find(self, needle: str, *, exact: bool = False, type_: str | None = None, index: int = 0,
             field_: str | None = None, tree: list[Element] | None = None) -> Element | None:
        el = super().find(needle, exact=exact, type_=type_, index=index, field_=field_, tree=tree)
        if el is not None or tree is not None:
            return el
        # Not in the flat tree: the target may be a navigation-bar button (see bar_elements).
        hits = [e for e in self.bar_elements() if e.matches(needle, exact=exact, type_=type_, field_=field_)]
        return hits[index] if len(hits) > index else None

    def screen_scale(self) -> float:
        """Screenshot pixels per point (3.0 on current iPhones); tap coordinates are in points."""
        r = run(["idb", "describe", "--udid", self.udid, "--json"], timeout=30)
        try:
            return float(json.loads(r.stdout).get("screen_dimensions", {}).get("density") or 3.0)
        except (json.JSONDecodeError, TypeError, ValueError):
            return 3.0

    # --- accessibility health --------------------------------------------------------
    def accessibility_healthy(self, app: str | None = None, wait_s: float = 8.0) -> bool:
        """True when the simulator exposes more than the bare Application node for the foreground app."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                if len(self.tree()) > 1:
                    return True
            except DeviceError:
                pass
            if time.monotonic() > deadline:
                return False
            time.sleep(1.0)

    def recover_accessibility(self, app: str | None = None, launch_args: list[str] | None = None) -> dict:
        """The simulator's accessibility bridge can wedge (observed after app (re)installs and XCUITest
        sessions): every app, even Settings, reports only its Application node. The same symptom appears while
        a system sheet that runs outside the app (Save Password?) is on screen; restarting backboardd closes it.

        Recovery, cheapest first: (1) restart the simulator's `backboardd` (the process that owns the
        accessibility server; ~8 s, apps are relaunched), (2) full simulator reboot (~30–40 s). The idb
        companion is restarted in both cases."""
        steps = []
        args = launch_args or []

        def restart_companion() -> None:
            run(["pkill", "-f", f"idb_companion.*{self.udid}"])
            run([self._idb or "idb", "disconnect", self.udid], timeout=30)

        # (1) backboardd restart
        self._simctl("spawn", self.udid, "launchctl", "kickstart", "-k", "system/com.apple.backboardd", check=False, timeout=60)
        steps.append("backboardd restarted")
        time.sleep(8)
        restart_companion(); steps.append("companion restarted")
        if app:
            self.launch(app, args); time.sleep(4); steps.append(f"relaunched {app}")
        if self.accessibility_healthy(app, wait_s=15):
            self._rec("recover-accessibility", ok=True, steps=steps)
            return {"ok": True, "steps": steps, "method": "backboardd"}
        # (2) full reboot
        restart_companion()
        self._simctl("shutdown", self.udid, check=False, timeout=120); steps.append("shutdown")
        self._simctl("boot", self.udid, check=False, timeout=120)
        self._simctl("bootstatus", self.udid, "-b", check=False, timeout=240); steps.append("booted")
        time.sleep(3)
        if app:
            self.launch(app, args); time.sleep(4); steps.append(f"relaunched {app}")
        ok = self.accessibility_healthy(app, wait_s=20)
        self._rec("recover-accessibility", ok=ok, steps=steps)
        return {"ok": ok, "steps": steps, "method": "reboot"}

    def crash_reports(self, executable: str, since_s: float = 3600) -> list[str]:
        d = Path.home() / "Library/Logs/DiagnosticReports"
        if not d.exists():
            return []
        cutoff = time.time() - since_s
        return sorted(str(p) for p in d.glob(f"{executable}*") if p.stat().st_mtime > cutoff)


def app_identity(bundle_path: Path) -> dict:
    """Stable identity of an .app: bundle id, version, and a sha256 of its main executable."""
    info = bundle_path / "Info.plist"
    ident: dict = {"path": str(bundle_path)}
    if info.exists():
        with open(info, "rb") as fh:
            pl = plistlib.load(fh)
        ident.update(bundle_id=pl.get("CFBundleIdentifier"), version=pl.get("CFBundleShortVersionString"),
                     build=pl.get("CFBundleVersion"), executable=pl.get("CFBundleExecutable"))
        exe = bundle_path / (pl.get("CFBundleExecutable") or "")
        if exe.exists():
            ident["sha256"] = sha256_file(exe)
    return ident


def list_simulators() -> list[dict]:
    r = run(["xcrun", "simctl", "list", "devices", "available", "-j"], timeout=30)
    out = []
    if not r.ok:
        return out
    for rt, devs in json.loads(r.stdout)["devices"].items():
        for d in devs:
            out.append({"udid": d["udid"], "name": d["name"], "state": d.get("state"), "runtime": rt.rsplit(".", 1)[-1]})
    return out


def ensure_simulator(name: str, device_type: str, runtime: str | None = None, boot: bool = True) -> dict:
    """Find-or-create a dedicated simulator by name; boot it; return {udid,name,created}."""
    for d in list_simulators():
        if d["name"] == name:
            udid = d["udid"]; created = False
            break
    else:
        if runtime is None:
            r = run(["xcrun", "simctl", "list", "runtimes", "-j"], timeout=30, check=True)
            rts = [x for x in json.loads(r.stdout)["runtimes"] if x["platform"] == "iOS" and x["isAvailable"]]
            if not rts:
                raise DeviceError("no available iOS runtime")
            runtime = sorted(rts, key=lambda x: [int(p) for p in x["version"].split(".")])[-1]["identifier"]
        r = run(["xcrun", "simctl", "create", name, device_type, runtime], timeout=60, check=True)
        udid = r.stdout.strip(); created = True
    if boot:
        run(["xcrun", "simctl", "boot", udid], timeout=60)  # ok if already booted
        run(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=180)
    return {"udid": udid, "name": name, "created": created}
