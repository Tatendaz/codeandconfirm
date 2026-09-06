"""`ccdevice` — the computer-use adapter the QA worker calls from a shell.

    ccdevice ios tree                       # accessibility tree (labels, values, ids, centers)
    ccdevice ios tap "Create account"       # tap by accessibility text/id (coordinate fallback: --xy X Y)
    ccdevice ios type "hello"               # type into the focused field
    ccdevice android wait-for "My trees" --timeout 20
    ccdevice android screenshot after-signup
    ccdevice ios launch --reset             # (re)launch the configured app with its launch args
    ccdevice ios app-state                  # foreground app + installed build identity
    ccdevice android logs --since 2m --grep Exception
    ccdevice ios proof                      # scripted self-test: tap, type, navigate, screenshots

Device, app and evidence directory come from flags or from the environment the coordinator
sets for a run: CAC_IOS_UDID, CAC_ANDROID_SERIAL, CAC_IOS_BUNDLE, CAC_ANDROID_PACKAGE,
CAC_ANDROID_ACTIVITY, CAC_IOS_APP_PATH, CAC_ANDROID_APK, CAC_IOS_LAUNCH_ARGS, CAC_EVIDENCE_DIR.
Every action is appended to <evidence>/actions.jsonl; screenshots are numbered in that directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path

from ..util import eprint
from .android_adb import AndroidEmulator, list_devices as android_list
from .base import Device, DeviceError, Evidence
from .ios_idb import IOSSimulator, list_simulators


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def make_device(platform: str, ident: str | None, evidence_dir: str | None) -> Device:
    if platform == "ios":
        udid = ident or _env("CAC_IOS_UDID")
        if not udid:
            raise DeviceError("no iOS device: pass --device <udid> or set CAC_IOS_UDID")
        ev = Evidence(Path(evidence_dir), "ios", udid) if evidence_dir else None
        return IOSSimulator(udid, ev)
    if platform == "android":
        serial = ident or _env("CAC_ANDROID_SERIAL")
        if not serial:
            raise DeviceError("no Android device: pass --device <serial> or set CAC_ANDROID_SERIAL")
        ev = Evidence(Path(evidence_dir), "android", serial) if evidence_dir else None
        return AndroidEmulator(serial, ev, sdk=_env("CAC_ANDROID_SDK"))
    raise DeviceError(f"unknown platform {platform!r} (ios|android)")


def app_for(platform: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    if platform == "ios":
        b = _env("CAC_IOS_BUNDLE")
        if not b:
            raise DeviceError("no app: pass --app <bundle-id> or set CAC_IOS_BUNDLE")
        return b
    pkg = _env("CAC_ANDROID_PACKAGE")
    if not pkg:
        raise DeviceError("no app: pass --app <package[/activity]> or set CAC_ANDROID_PACKAGE")
    return f"{pkg}/{_env('CAC_ANDROID_ACTIVITY', '.MainActivity')}"


def launch_args_for(platform: str, reset: bool = False) -> list[str]:
    """Always-on launch args, plus the reset args (e.g. sign-out-on-launch flags) only when `reset` is set."""
    pre = "CAC_IOS" if platform == "ios" else "CAC_ANDROID"
    args = shlex.split(_env(f"{pre}_LAUNCH_ARGS", "") or "")
    if reset:
        args += shlex.split(_env(f"{pre}_RESET_ARGS", "") or "")
    return args


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ccdevice", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("platform", choices=["ios", "android", "devices", "env"], help="platform, or 'devices'/'env' to list")
    p.add_argument("command", nargs="?", help="tree|find|tap|type|key|back|home|scroll|screenshot|wait-for|launch|terminate|restart|install|uninstall|clear-data|app-state|logs|dismiss-keyboard|memory|proof|ax-health|recover-accessibility|describe-point|scale|bar")
    p.add_argument("args", nargs="*")
    p.add_argument("--device", help="UDID (ios) or serial (android); default from env")
    p.add_argument("--app", help="bundle id (ios) or package[/activity] (android); default from env")
    p.add_argument("--evidence", default=_env("CAC_EVIDENCE_DIR"), help="evidence directory (default $CAC_EVIDENCE_DIR)")
    p.add_argument("--exact", action="store_true", help="match text exactly instead of contains")
    p.add_argument("--type", dest="type_", help="restrict matches to an element type (Button, TextField, EditText...)")
    p.add_argument("--field", dest="field_", choices=["label", "value", "id"], help="match only this field")
    p.add_argument("--index", type=int, default=0, help="use the Nth match (0-based)")
    p.add_argument("--xy", nargs=2, type=int, metavar=("X", "Y"), help="coordinate fallback for tap")
    p.add_argument("--snap", action="store_true", help="take a screenshot after the action")
    p.add_argument("--times", type=int, default=1, help="tap: number of rapid taps delivered from one process (double-tap = 2)")
    p.add_argument("--interval-ms", type=int, default=80, help="tap: milliseconds between rapid taps")
    p.add_argument("--anchor", choices=["auto", "center", "trailing", "leading"], default="auto",
                   help="tap: where inside the element to tap (auto = trailing edge for switches/checkboxes)")
    p.add_argument("--timeout", type=float, default=10.0, help="seconds for wait-for")
    p.add_argument("--gone", action="store_true", help="wait-for: wait until the element disappears")
    p.add_argument("--since", default="2m", help="logs: window, e.g. 30s, 2m, 1h")
    p.add_argument("--grep", help="logs/tree: filter lines/elements containing text")
    p.add_argument("--all", dest="all_", action="store_true", help="tree: include unlabeled elements")
    p.add_argument("--reset", action="store_true", help="launch: clear app data and add the configured reset args (fresh signed-out state); restart never resets")
    p.add_argument("--base", action="store_true", help="install: install the BASE build for comparison (finish with `install --candidate`)")
    p.add_argument("--candidate", action="store_true", help="install: (re)install the candidate build (default)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    ns = ap.parse_args(argv)
    try:
        return _dispatch(ns)
    except DeviceError as e:
        eprint(f"ccdevice: {e}")
        return 2
    except KeyboardInterrupt:
        return 130


def _out(ns, data, text: str | None = None) -> None:
    if ns.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(text if text is not None else json.dumps(data, indent=2, default=str))


def _dispatch(ns) -> int:
    if ns.platform == "devices":
        data = {"ios": list_simulators(), "android": android_list(_env("CAC_ANDROID_SDK"))}
        _out(ns, data, "\n".join([f"ios     {d['udid']}  {d['state']:<9} {d['name']} ({d['runtime']})" for d in data["ios"]]
                                 + [f"android {d['serial']}  {d['state']:<9} {d['info']}" for d in data["android"]]))
        return 0
    if ns.platform == "env":
        keys = [k for k in os.environ if k.startswith("CAC_")]
        _out(ns, {k: os.environ[k] for k in sorted(keys)}, "\n".join(f"{k}={os.environ[k]}" for k in sorted(keys)))
        return 0
    if not ns.command:
        eprint("ccdevice: missing command"); return 2
    dev = make_device(ns.platform, ns.device, ns.evidence)
    cmd = ns.command
    a = ns.args

    if cmd == "tree":
        if ns.json:
            print(json.dumps([e.to_dict() for e in dev.tree()], default=str))
        else:
            print(dev.compact_tree(type_=ns.type_, grep=ns.grep, all_=ns.all_))
        return 0
    if cmd == "find":
        el = dev.find(_need(a, "text"), exact=ns.exact, type_=ns.type_, index=ns.index, field_=ns.field_)
        if el is None:
            eprint("not found"); return 1
        _out(ns, el.to_dict(), el.short()); return 0
    if cmd == "tap":
        el = dev.tap(a[0] if a else None, xy=tuple(ns.xy) if ns.xy else None, exact=ns.exact, type_=ns.type_,
                     index=ns.index, field_=ns.field_, snap=ns.snap, times=ns.times, interval_ms=ns.interval_ms, anchor=ns.anchor)
        _out(ns, {"tapped": el.to_dict() if el else {"xy": ns.xy}, "times": ns.times},
             f"tapped{' x' + str(ns.times) if ns.times > 1 else ''} {el.short() if el else ns.xy}")
        return 0
    if cmd == "type":
        dev.type(" ".join(a), snap=ns.snap); print("typed"); return 0
    if cmd == "key":
        dev.press_key(_need(a, "key")); print("ok"); return 0
    if cmd == "back":
        dev.back(); print("ok"); return 0
    if cmd == "home":
        dev.home(); print("ok"); return 0
    if cmd == "scroll":
        dev.scroll(a[0] if a else "down"); print("ok"); return 0
    if cmd == "screenshot":
        p = dev.snap(a[0] if a else "shot") if dev.evidence else dev.screenshot(Path(a[0] if a else "screenshot.png"))
        print(p); return 0
    if cmd == "wait-for":
        t0 = time.monotonic()
        el = dev.wait_for(_need(a, "text"), ns.timeout, gone=ns.gone, exact=ns.exact, type_=ns.type_)
        took = round(time.monotonic() - t0, 2)
        if ns.gone:
            ok = el is None
        else:
            ok = el is not None
        dev._rec("wait-for", ok=ok, target=a[0], gone=ns.gone, seconds=took)
        if ok:
            _out(ns, {"ok": True, "seconds": took, "element": el.to_dict() if el else None},
                 f"{'gone' if ns.gone else 'found'} after {took}s" + (f": {el.short()}" if el else ""))
            return 0
        eprint(f"timeout after {took}s waiting for {a[0]!r}{' to disappear' if ns.gone else ''}")
        return 1
    if cmd == "launch":
        app = app_for(ns.platform, ns.app)
        if ns.reset:
            dev.clear_data(app)
        info = dev.launch(app, launch_args_for(ns.platform, reset=ns.reset))
        _out(ns, info); return 0
    if cmd == "terminate":
        dev.terminate(app_for(ns.platform, ns.app)); print("ok"); return 0
    if cmd == "restart":
        app = app_for(ns.platform, ns.app)
        dev.terminate(app); time.sleep(1.0)
        _out(ns, dev.launch(app, launch_args_for(ns.platform))); return 0
    if cmd == "install":
        which_build = "base" if ns.base else "candidate"
        if a:
            path = a[0]
        elif ns.base:
            path = _env("CAC_IOS_BASE_APP_PATH" if ns.platform == "ios" else "CAC_ANDROID_BASE_APK")
            if not path:
                raise DeviceError("no base build is available in this run")
        else:
            path = _env("CAC_IOS_APP_PATH" if ns.platform == "ios" else "CAC_ANDROID_APK")
        if not path:
            raise DeviceError("install needs a path (or CAC_IOS_APP_PATH / CAC_ANDROID_APK)")
        app = app_for(ns.platform, ns.app)
        dev.uninstall(app)
        info = dev.install(Path(path))
        info["build"] = which_build
        dev._rec("install-switch", build=which_build, sha256=info.get("sha256"))
        _out(ns, info); return 0
    if cmd == "uninstall":
        dev.uninstall(app_for(ns.platform, ns.app)); print("ok"); return 0
    if cmd == "clear-data":
        dev.clear_data(app_for(ns.platform, ns.app)); print("ok"); return 0
    if cmd == "app-state":
        _out(ns, dev.app_state(app_for(ns.platform, ns.app))); return 0
    if cmd == "logs":
        print(dev.logs(app_for(ns.platform, ns.app), since=ns.since, grep=ns.grep)); return 0
    if cmd == "dismiss-keyboard":
        dev.dismiss_keyboard(); print("ok"); return 0
    if cmd == "memory":
        kb = dev.memory_kb(app_for(ns.platform, ns.app))
        _out(ns, {"memory_kb": kb}, f"{kb} kB" if kb else "unknown"); return 0
    if cmd == "proof":
        rc = proof(dev, ns)
        if rc != 0 and ns.platform == "ios" and "read-ui" in json.dumps(read_proof(dev)):
            eprint("ccdevice: empty accessibility tree on iOS; recovering the simulator once and retrying the proof")
            dev.recover_accessibility(app_for("ios", ns.app), launch_args_for("ios", reset=True))
            rc = proof(dev, ns)
        return rc
    if cmd == "recover-accessibility":
        if ns.platform != "ios":
            eprint("recover-accessibility is iOS-only"); return 2
        _out(ns, dev.recover_accessibility(app_for("ios", ns.app), launch_args_for("ios")))
        return 0
    if cmd == "bar":
        if ns.platform != "ios":
            eprint("bar is iOS-only"); return 2
        els = dev.bar_elements()
        _out(ns, [e.to_dict() for e in els], "\n".join(e.short() for e in els) or "(no bar buttons found)"); return 0
    if cmd == "describe-point":
        if ns.platform != "ios":
            eprint("describe-point is iOS-only"); return 2
        x, y = int(_need(a, "x")), int(a[1]) if len(a) > 1 else 0
        _out(ns, dev.describe_point(x, y)); return 0
    if cmd == "scale":
        sc = dev.screen_scale() if ns.platform == "ios" else 1.0
        w, h = dev.screen_size()
        _out(ns, {"points_per_screenshot_pixel": 1 / sc, "screen_points": [w, h]},
             f"tap coordinates are in {'points' if ns.platform == 'ios' else 'pixels'}; screenshot pixels ÷ {sc:g} = tap coordinates; screen {w}x{h}")
        return 0
    if cmd == "ax-health":
        ok = dev.accessibility_healthy() if ns.platform == "ios" else len(dev.tree()) > 1
        _out(ns, {"healthy": ok}, "healthy" if ok else "UNHEALTHY: only the application node is exposed")
        return 0 if ok else 1
    eprint(f"ccdevice: unknown command {cmd!r}"); return 2


def read_proof(dev: Device) -> dict:
    if dev.evidence and (dev.evidence.dir / "proof.json").exists():
        try:
            return json.loads((dev.evidence.dir / "proof.json").read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _need(a: list[str], what: str) -> str:
    if not a:
        raise DeviceError(f"missing {what}")
    return a[0]


def proof(dev: Device, ns) -> int:
    """Scripted self-test: launch, read UI, tap a text field, type, verify, navigate away and back.

    Proves the adapter can drive THIS device and THIS build; the coordinator runs it before
    handing the device to the QA worker so a broken adapter shows up as BLOCKED, not as a
    silent lack of coverage.
    """
    platform = dev.platform
    app = app_for(platform, ns.app)
    results: list[dict] = []

    def step(name: str, fn):
        t0 = time.monotonic()
        try:
            detail = fn()
            ok = True
        except Exception as e:  # noqa: BLE001
            detail = str(e); ok = False
        results.append({"step": name, "ok": ok, "detail": detail, "seconds": round(time.monotonic() - t0, 2)})
        eprint(f"  {'ok ' if ok else 'FAIL'} {name}: {str(detail)[:120]}")
        if not ok:
            raise DeviceError(f"proof step failed: {name}: {detail}")

    try:
        step("app-state", lambda: dev.app_state(app))
        step("launch", lambda: dev.launch(app, launch_args_for(platform, reset=True)))
        time.sleep(3)
        step("screenshot-1", lambda: str(dev.snap("proof-launch")))

        def read_tree():
            els = dev.tree()
            if len(els) < 3:
                raise DeviceError(f"only {len(els)} elements in tree")
            return f"{len(els)} elements"
        step("read-ui", read_tree)

        def tap_field():
            els = dev.tree()
            f = next((e for e in els if e.type in ("TextField", "SecureTextField", "EditText")), None)
            if f is None:
                raise DeviceError("no text field on screen")
            dev.tap(xy=f.center)
            return f.short()
        step("tap-text-field", tap_field)
        marker = "cac proof 7731"
        step("type", lambda: dev.type(marker) or marker)

        def verify_typed():
            els = dev.tree()
            if any(marker.lower() in (e.value or "").lower() or marker.lower() in (e.label or "").lower() for e in els):
                return "typed text visible in accessibility tree"
            raise DeviceError("typed text not found in tree")
        step("verify-typed", verify_typed)
        step("screenshot-2", lambda: str(dev.snap("proof-typed")))
        step("dismiss-keyboard", lambda: dev.dismiss_keyboard() or "ok")

        def navigate():
            before = {e.haystack() for e in dev.tree()}
            els = dev.tree()
            # Prefer a labeled, enabled button that is not the primary submit button.
            cands = [e for e in els if e.type in ("Button", "TextView", "View") and e.enabled and e.label
                     and e.label.lower() not in ("create account", "sign in", "submit", "continue")
                     and not e.label.lower().startswith("continue with")]
            if not cands:
                raise DeviceError("no navigation control found")
            target = cands[-1]
            dev.tap(xy=target.center)
            time.sleep(1.2)
            after = {e.haystack() for e in dev.tree()}
            if before == after:
                raise DeviceError(f"UI did not change after tapping {target.label!r}")
            return f"tapped {target.label!r}; UI changed ({len(after ^ before)} elements differ)"
        step("navigate", navigate)
        step("screenshot-3", lambda: str(dev.snap("proof-navigated")))

        def go_back():
            before = {e.haystack() for e in dev.tree()}
            dev.back()
            time.sleep(1.2)
            after = {e.haystack() for e in dev.tree()}
            return "UI changed after back" if before != after else "back pressed (UI unchanged)"
        step("back", go_back)
        step("screenshot-4", lambda: str(dev.snap("proof-back")))
        step("app-state-final", lambda: dev.app_state(app))
    except DeviceError as e:
        summary = {"platform": platform, "device": dev.ident, "ok": False, "error": str(e), "steps": results}
        if dev.evidence:
            (dev.evidence.dir / "proof.json").write_text(json.dumps(summary, indent=2, default=str))
        _out(ns, summary, f"PROOF FAILED on {platform} {dev.ident}: {e}")
        return 1
    summary = {"platform": platform, "device": dev.ident, "ok": True, "steps": results,
               "evidence": dev.evidence.summary() if dev.evidence else None}
    if dev.evidence:
        (dev.evidence.dir / "proof.json").write_text(json.dumps(summary, indent=2, default=str))
    _out(ns, summary, f"PROOF OK on {platform} {dev.ident}: {len(results)} steps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
