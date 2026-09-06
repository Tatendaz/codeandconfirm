"""Android emulator adapter: `adb` for everything, `uiautomator dump` for the accessibility tree.

Fully device-scoped (adb -s <serial>), no desktop involvement, works headless.
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ..util import run, sha256_file, expand
from .base import Device, DeviceError, Element, Evidence

KEYCODES = {"back": 4, "home": 3, "return": 66, "enter": 66, "escape": 111, "tab": 61, "delete": 67,
            "backspace": 67, "menu": 82, "power": 26, "space": 62}


def sdk_root(explicit: str | None = None) -> Path:
    for cand in (explicit, os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"), "~/Library/Android/sdk"):
        if cand and (expand(cand) / "platform-tools" / "adb").exists():
            return expand(cand)
    raise DeviceError("Android SDK not found (set ANDROID_HOME or tools.android_sdk)")


class AndroidEmulator(Device):
    platform = "android"

    def __init__(self, serial: str, evidence: Evidence | None = None, sdk: str | None = None, timeout: float = 60):
        super().__init__(serial, evidence)
        self.serial = serial
        self.sdk = sdk_root(sdk)
        self.adb = str(self.sdk / "platform-tools" / "adb")
        self.timeout = timeout

    def _adb(self, *args: str, timeout: float | None = None, check: bool = True, binary: bool = False):
        argv = [self.adb, "-s", self.serial, *args]
        r = run(argv, timeout=timeout or self.timeout)
        if check and not r.ok:
            raise DeviceError(f"adb {' '.join(args[:2])} failed (exit {r.exit_code}): {(r.stderr or r.stdout).strip()[:400]}")
        return r

    def _shell(self, *args: str, timeout: float | None = None, check: bool = True) -> str:
        return self._adb("shell", *args, timeout=timeout, check=check).stdout

    def available(self) -> tuple[bool, str]:
        r = run([self.adb, "devices"], timeout=30)
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == self.serial:
                return parts[1] == "device", f"state={parts[1]}"
        return False, "serial not listed"

    # --- primitives -----------------------------------------------------------
    def tree(self) -> list[Element]:
        self._shell("uiautomator", "dump", "/sdcard/cac-ui.xml", timeout=60)
        r = self._adb("exec-out", "cat", "/sdcard/cac-ui.xml", timeout=30)
        xml = r.stdout
        if "<!DOCTYPE" in xml or "<!ENTITY" in xml:   # never expand entities from device-supplied XML
            raise DeviceError("uiautomator dump contains a DTD/entity declaration; refusing to parse")
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            raise DeviceError(f"uiautomator dump unparsable: {xml[:200]!r}") from e
        els = []
        for i, n in enumerate(root.iter("node")):
            m = re.findall(r"-?\d+", n.get("bounds", "[0,0][0,0]"))
            x1, y1, x2, y2 = (int(v) for v in m[:4]) if len(m) >= 4 else (0, 0, 0, 0)
            rid = n.get("resource-id", "")
            desc = n.get("content-desc", "")
            cls = (n.get("class") or "").rsplit(".", 1)[-1]
            els.append(Element(
                index=i, type=cls, label=n.get("text", "") or desc, value=n.get("text", "") if cls == "EditText" else "",
                identifier=rid or desc, enabled=n.get("enabled", "true") == "true",
                x=x1, y=y1, w=max(0, x2 - x1), h=max(0, y2 - y1),
                extra={"clickable": n.get("clickable") == "true", "focused": n.get("focused") == "true",
                       "scrollable": n.get("scrollable") == "true", "content_desc": desc, "resource_id": rid,
                       "package": n.get("package", "")},
            ))
        return els

    def screenshot(self, path: Path) -> Path:
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        r = run([self.adb, "-s", self.serial, "exec-out", "screencap", "-p"], timeout=30)
        raw = subprocess_bytes(self.adb, self.serial)
        path.write_bytes(raw)
        if path.stat().st_size < 1000:
            raise DeviceError("screencap produced an empty image")
        return path

    def tap_xy(self, x: int, y: int) -> None:
        self._shell("input", "tap", str(x), str(y))

    def type_text(self, text: str) -> None:
        # `input text` takes a single argument; escape shell-special characters and spaces.
        esc = re.sub(r"([\\()<>|;&*~\"'`$])", r"\\\1", text).replace(" ", "%s")
        self._shell("input", "text", esc)

    def key(self, name: str) -> None:
        code = KEYCODES.get(name.lower())
        if code is None and name.isdigit():
            code = int(name)
        if code is None:
            raise DeviceError(f"unknown key {name!r}; known: {sorted(KEYCODES)}")
        self._shell("input", "keyevent", str(code))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def launch(self, app: str, args: list[str] | None = None) -> dict:
        pkg, activity = _split_component(app)
        extra: list[str] = []
        for a in args or []:
            if "=" in a:
                k, v = a.split("=", 1); extra += ["--es", k, v]
            else:
                extra += ["--ez", a, "true"]
        out = self._shell("am", "start", "-W", "-S", "-n", f"{pkg}/{activity}", *extra, timeout=90)
        info = {"args": args or []}
        m = re.search(r"TotalTime:\s*(\d+)", out)
        if m:
            info["startup_total_ms"] = int(m.group(1))
        m = re.search(r"Status:\s*(\w+)", out)
        info["status"] = m.group(1) if m else "unknown"
        info["pid"] = self._pid(pkg)
        self._rec("launch", app=pkg, **info)
        return info

    def terminate(self, app: str) -> None:
        pkg, _ = _split_component(app)
        self._shell("am", "force-stop", pkg)
        self._rec("terminate", app=pkg)

    def install(self, path: Path) -> dict:
        path = Path(path)
        if not path.exists():
            raise DeviceError(f"apk not found: {path}")
        r = self._adb("install", "-r", "-g", str(path), timeout=300)
        if "Success" not in r.stdout:
            raise DeviceError(f"adb install did not report Success: {r.stdout[-300]}")
        ident = apk_identity(path, self.sdk)
        self._rec("install", app=ident.get("package"), **ident)
        return ident

    def uninstall(self, app: str) -> None:
        pkg, _ = _split_component(app)
        self._adb("uninstall", pkg, check=False)
        self._rec("uninstall", app=pkg)

    def clear_data(self, app: str) -> None:
        pkg, _ = _split_component(app)
        self._shell("pm", "clear", pkg)
        self._rec("clear-data", app=pkg)

    def app_state(self, app: str) -> dict:
        pkg, _ = _split_component(app)
        path = self._shell("pm", "path", pkg, check=False).strip().replace("package:", "")
        state = {"installed": bool(path), "apk_path": path or None, "identity": None, "foreground": None}
        if path:
            sha = self._shell("sha256sum", path, check=False).split()
            ver = re.search(r"versionName=(\S+)", self._shell("dumpsys", "package", pkg, check=False))
            state["identity"] = {"package": pkg, "sha256": sha[0] if sha else None, "version": ver.group(1) if ver else None}
        act = self._shell("dumpsys", "activity", "activities", check=False)
        m = re.search(r"(?:topResumedActivity|mResumedActivity)[^\n]*?(\S+)/(\S+)\s", act)
        if m:
            state["foreground"] = {"package": m.group(1), "activity": m.group(2), "is_target": m.group(1) == pkg,
                                   "pid": self._pid(pkg)}
        return state

    def logs(self, app: str, since: str = "2m", grep: str | None = None) -> str:
        pkg, _ = _split_component(app)
        pid = self._pid(pkg)
        args = ["logcat", "-d", "-v", "time", "-T", _since_to_logcat(since)]
        if pid:
            args += ["--pid", str(pid)]
        out = self._adb(*args, timeout=60, check=False).stdout
        if grep:
            out = "\n".join(l for l in out.splitlines() if grep.lower() in l.lower())
        return out[-20000:]

    def keyboard_shown(self) -> bool | None:
        out = self._shell("dumpsys", "input_method", check=False)
        m = re.search(r"mInputShown=(true|false)", out)
        return (m.group(1) == "true") if m else None

    def dismiss_keyboard(self) -> None:
        if self.keyboard_shown():
            self.key("escape")
            time.sleep(0.4)
        if self.keyboard_shown():
            self.key("back")
        self._rec("dismiss-keyboard", shown_after=self.keyboard_shown())

    def screen_size(self) -> tuple[int, int]:
        m = re.search(r"(\d+)x(\d+)", self._shell("wm", "size"))
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)

    def memory_kb(self, app: str) -> int | None:
        pkg, _ = _split_component(app)
        out = self._shell("dumpsys", "meminfo", pkg, check=False)
        m = re.search(r"TOTAL PSS:\s*(\d+)", out) or re.search(r"TOTAL\s+(\d+)", out)
        return int(m.group(1)) if m else None

    def _pid(self, pkg: str) -> int | None:
        out = self._shell("pidof", pkg, check=False).strip()
        return int(out.split()[0]) if out else None

    def crash_reports(self, pkg: str) -> str:
        return self._adb("logcat", "-d", "-b", "crash", timeout=30, check=False).stdout[-10000:]

    # Foreign apps a previous session may have left in the foreground; a focused foreign window makes
    # instrumentation touch injection fail ("Failed to inject touch input") for the app under test.
    FOREIGN_FOREGROUND_APPS = ("com.android.settings", "com.google.android.apps.nexuslauncher.settings")

    def set_test_friendly(self) -> None:
        """Animations off, screen awake and unlocked, foreign apps out of the foreground. Idempotent; run before
        every install, not only after a boot, because an emulator that stayed up keeps yesterday's state."""
        for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            self._shell("settings", "put", "global", k, "0", check=False)
        self._shell("input", "keyevent", "KEYCODE_WAKEUP", check=False)
        self._shell("svc", "power", "stayon", "true", check=False)
        self._shell("wm", "dismiss-keyguard", check=False)
        for pkg in self.FOREIGN_FOREGROUND_APPS:
            self._shell("am", "force-stop", pkg, check=False)
        self._shell("input", "keyevent", "KEYCODE_HOME", check=False)


def subprocess_bytes(adb: str, serial: str) -> bytes:
    import subprocess
    return subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=30).stdout


def _split_component(app: str) -> tuple[str, str]:
    if "/" in app:
        pkg, act = app.split("/", 1)
        return pkg, act
    return app, ".MainActivity"


def _since_to_logcat(since: str) -> str:
    m = re.match(r"(\d+)([smh])", since)
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] if m else 120
    return time.strftime("%m-%d %H:%M:%S.000", time.localtime(time.time() - secs))


def apk_identity(apk: Path, sdk: Path) -> dict:
    ident: dict = {"path": str(apk), "sha256": sha256_file(apk)}
    aapts = sorted((sdk / "build-tools").glob("*/aapt2"), reverse=True) if (sdk / "build-tools").exists() else []
    if aapts:
        r = run([str(aapts[0]), "dump", "badging", str(apk)], timeout=60)
        m = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", r.stdout)
        if m:
            ident.update(package=m.group(1), version_code=int(m.group(2)), version=m.group(3))
    return ident


def list_devices(sdk: str | None = None) -> list[dict]:
    root = sdk_root(sdk)
    r = run([str(root / "platform-tools" / "adb"), "devices", "-l"], timeout=30)
    out = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out.append({"serial": parts[0], "state": parts[1], "info": " ".join(parts[2:])})
    return out


def ensure_avd(name: str, *, source_avd: str | None = None, system_image: str | None = None, sdk: str | None = None) -> dict:
    """Find-or-create a dedicated AVD. Uses avdmanager when present; otherwise clones an
    existing AVD's config.ini (works without cmdline-tools)."""
    root = sdk_root(sdk)
    emulator = root / "emulator" / "emulator"
    r = run([str(emulator), "-list-avds"], timeout=60)
    if name in r.stdout.split():
        return {"name": name, "created": False}
    avd_home = expand(os.environ.get("ANDROID_AVD_HOME") or "~/.android/avd")
    avdmanagers = sorted((root / "cmdline-tools").glob("*/bin/avdmanager")) if (root / "cmdline-tools").exists() else []
    if avdmanagers and system_image:
        run([str(avdmanagers[-1]), "create", "avd", "-n", name, "-k", system_image, "--device", "pixel_6", "--force"],
            input_text="no\n", timeout=300, check=True)
        return {"name": name, "created": True, "method": "avdmanager"}
    src = source_avd or next((a for a in r.stdout.split() if a != name), None)
    if not src:
        raise DeviceError("no AVD to clone and no avdmanager/system image to create one")
    cfg = (avd_home / f"{src}.avd" / "config.ini").read_text()
    cfg = re.sub(r"^AvdId=.*$", f"AvdId={name}", cfg, flags=re.M)
    cfg = re.sub(r"^avd\.ini\.displayname=.*$", f"avd.ini.displayname={name}", cfg, flags=re.M)
    cfg = re.sub(r"^snapshot\..*\n", "", cfg, flags=re.M)
    (avd_home / f"{name}.avd").mkdir(parents=True, exist_ok=True)
    (avd_home / f"{name}.avd" / "config.ini").write_text(cfg)
    target = re.search(r"system-images/(android-\d+)/", cfg)
    (avd_home / f"{name}.ini").write_text(
        f"avd.ini.encoding=UTF-8\npath={avd_home / (name + '.avd')}\npath.rel=avd/{name}.avd\ntarget={target.group(1) if target else 'android-34'}\n")
    return {"name": name, "created": True, "method": "clone", "source": src}


def boot_emulator(avd: str, port: int, *, headless: bool = False, log_file: Path | None = None, sdk: str | None = None,
                  timeout_s: float = 240) -> dict:
    """Start an emulator on a fixed console port (serial emulator-<port>) and wait for boot."""
    import subprocess
    root = sdk_root(sdk)
    serial = f"emulator-{port}"
    adb = str(root / "platform-tools" / "adb")
    if AndroidEmulator(serial, sdk=str(root)).available()[0]:
        return {"serial": serial, "pid": None, "already_running": True}
    args = [str(root / "emulator" / "emulator"), "-avd", avd, "-port", str(port), "-no-snapshot", "-no-boot-anim",
            "-no-audio", "-netdelay", "none", "-netspeed", "full"]
    if headless:
        args += ["-no-window", "-gpu", "swiftshader_indirect"]
    lf = open(log_file, "ab") if log_file else subprocess.DEVNULL
    proc = subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + timeout_s
    run([adb, "-s", serial, "wait-for-device"], timeout=timeout_s)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise DeviceError(f"emulator exited early (code {proc.returncode}); see {log_file}")
        out = run([adb, "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=15).stdout.strip()
        if out == "1":
            dev = AndroidEmulator(serial, sdk=str(root))
            dev.set_test_friendly()
            return {"serial": serial, "pid": proc.pid, "already_running": False}
        time.sleep(2)
    raise DeviceError(f"emulator {avd} did not finish booting within {timeout_s}s")
