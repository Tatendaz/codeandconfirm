from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from ..util import append_jsonl, now_iso, read_jsonl


class DeviceError(RuntimeError):
    pass


@dataclass
class Element:
    index: int
    type: str
    label: str = ""
    value: str = ""
    identifier: str = ""
    enabled: bool = True
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    extra: dict = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int]:
        return int(self.x + self.w / 2), int(self.y + self.h / 2)

    def haystack(self) -> str:
        return "".join([self.label or "", self.value or "", self.identifier or ""])

    def matches(self, needle: str, *, exact: bool = False, type_: str | None = None, field_: str | None = None) -> bool:
        if type_ and self.type.lower() != type_.lower():
            return False
        fields = {"label": self.label, "value": self.value, "id": self.identifier}
        cands = [fields[field_]] if field_ else list(fields.values())
        for c in cands:
            c = c or ""
            if (c == needle) if exact else (needle in c):
                return True
        return False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["center"] = list(self.center)
        return d

    def short(self) -> str:
        cx, cy = self.center
        lab = (self.label or "")[:38]
        val = (self.value or "")[:24]
        ident = (self.identifier or "")[:28]
        en = "" if self.enabled else " DISABLED"
        return f"[{self.index:3}] {self.type:<14} {lab!r:<40} val={val!r:<26} id={ident!r:<30} @({cx},{cy}){en}"


INTERACTION_ACTIONS = {"tap", "type", "key", "back", "home", "scroll", "swipe", "dismiss-keyboard"}


class Evidence:
    """Append-only record of what was done to which device, plus numbered screenshots."""

    def __init__(self, directory: Path, platform: str, device: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.platform = platform
        self.device = device
        self.log = self.dir / "actions.jsonl"

    def _next_seq(self) -> int:
        return len(read_jsonl(self.log)) + 1

    def record(self, action: str, ok: bool = True, **data: Any) -> dict:
        rec = {"ts": now_iso(), "seq": self._next_seq(), "platform": self.platform, "device": self.device,
               "action": action, "ok": ok, **data}
        append_jsonl(self.log, rec)
        return rec

    def screenshot_path(self, name: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "shot"
        return self.dir / f"{self._next_seq():03d}-{slug}.png"

    def summary(self) -> dict:
        recs = read_jsonl(self.log)
        kinds: dict[str, int] = {}
        for r in recs:
            kinds[r.get("action")] = kinds.get(r.get("action"), 0) + 1
        interactions = sum(v for k, v in kinds.items() if k in INTERACTION_ACTIONS)
        shots = sorted(p.name for p in self.dir.glob("*.png"))
        return {"actions": len(recs), "by_action": kinds, "interactions": interactions, "screenshots": shots,
                "first_ts": recs[0]["ts"] if recs else None, "last_ts": recs[-1]["ts"] if recs else None}


class Device(ABC):
    platform: str = ""
    settle_s: float = 0.4   # pause after input so the UI can react before the next read

    def __init__(self, ident: str, evidence: Evidence | None = None):
        self.ident = ident
        self.evidence = evidence

    # --- abstract primitives ------------------------------------------------
    @abstractmethod
    def tree(self) -> list[Element]: ...

    @abstractmethod
    def screenshot(self, path: Path) -> Path: ...

    @abstractmethod
    def tap_xy(self, x: int, y: int) -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...

    @abstractmethod
    def key(self, name: str) -> None: ...

    @abstractmethod
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...

    @abstractmethod
    def launch(self, app: str, args: list[str] | None = None) -> dict: ...

    @abstractmethod
    def terminate(self, app: str) -> None: ...

    @abstractmethod
    def install(self, path: Path) -> dict: ...

    @abstractmethod
    def uninstall(self, app: str) -> None: ...

    @abstractmethod
    def clear_data(self, app: str) -> None: ...

    @abstractmethod
    def app_state(self, app: str) -> dict: ...

    @abstractmethod
    def logs(self, app: str, since: str = "2m", grep: str | None = None) -> str: ...

    @abstractmethod
    def keyboard_shown(self) -> bool | None: ...

    @abstractmethod
    def dismiss_keyboard(self) -> None: ...

    @abstractmethod
    def screen_size(self) -> tuple[int, int]: ...

    @abstractmethod
    def memory_kb(self, app: str) -> int | None: ...

    # --- helpers on top of primitives ---------------------------------------
    def _rec(self, action: str, ok: bool = True, **data: Any) -> None:
        if self.evidence:
            self.evidence.record(action, ok, **data)

    def snap(self, name: str) -> Path | None:
        if not self.evidence:
            return None
        p = self.evidence.screenshot_path(name)
        self.screenshot(p)
        self._rec("screenshot", file=p.name, name=name)
        return p

    def find(self, needle: str, *, exact: bool = False, type_: str | None = None, index: int = 0,
             field_: str | None = None, tree: list[Element] | None = None) -> Element | None:
        els = tree if tree is not None else self.tree()
        # The root Application/Window node carries the app's name; it is never a sensible tap target.
        cands = els if type_ else [e for e in els if e.type not in ("Application", "Window")]
        hits = [e for e in cands if e.matches(needle, exact=exact, type_=type_, field_=field_)]
        return hits[index] if len(hits) > index else None

    def wait_for(self, needle: str, timeout_s: float = 10, *, gone: bool = False, exact: bool = False,
                 type_: str | None = None, poll_s: float = 0.5) -> Element | None:
        deadline = time.monotonic() + timeout_s
        last = None
        while True:
            try:
                last = self.find(needle, exact=exact, type_=type_)
            except DeviceError:
                last = None
            if gone and last is None:
                return None
            if not gone and last is not None:
                return last
            if time.monotonic() > deadline:
                return last
            time.sleep(poll_s)

    def tap(self, needle: str | None = None, *, xy: tuple[int, int] | None = None, exact: bool = False,
            type_: str | None = None, index: int = 0, field_: str | None = None, snap: bool = False,
            times: int = 1, interval_ms: int = 80, anchor: str = "auto") -> Element | None:
        """Tap an element (by accessibility text/id) or a coordinate. `times` > 1 delivers a rapid multi-tap
        from ONE process (the coordinates are resolved once, so the taps are `interval_ms` apart — this is how
        a real double-tap is reproduced). `anchor` picks where inside the element to tap: auto (control types
        such as switches/checkboxes are tapped near their trailing edge, everything else at the center),
        center, trailing, leading."""
        el = None
        if xy is None:
            if needle is None:
                raise DeviceError("tap needs a target text/id or --xy")
            el = self.find(needle, exact=exact, type_=type_, index=index, field_=field_)
            if el is None:
                self._rec("tap", ok=False, target=needle, error="not found")
                raise DeviceError(f"no element matching {needle!r}")
            xy = self.tap_point(el, anchor)
        for i in range(max(1, times)):
            self.tap_xy(*xy)
            if i + 1 < times:
                time.sleep(interval_ms / 1000.0)
        time.sleep(self.settle_s)
        self._rec("tap", target=needle, xy=list(xy), element=el.to_dict() if el else None, times=times,
                  interval_ms=interval_ms if times > 1 else None, anchor=anchor,
                  mode="coordinate" if el is None else "accessibility")
        if snap:
            self.snap(f"after-tap-{needle or 'xy'}")
        return el

    CONTROL_TYPES = {"switch", "checkbox", "toggle", "togglebutton"}

    def tap_point(self, el: Element, anchor: str = "auto") -> tuple[int, int]:
        """Where to tap inside an element. SwiftUI/Compose toggles expose the whole row as one element whose
        control sits at the trailing edge; tapping the row's center hits the label and does nothing."""
        cx, cy = el.center
        if anchor == "auto":
            anchor = "trailing" if el.type.lower() in self.CONTROL_TYPES and el.w > 120 else "center"
        if anchor == "trailing":
            return int(el.x + el.w - min(28, el.w / 4)), cy
        if anchor == "leading":
            return int(el.x + min(28, el.w / 4)), cy
        return cx, cy

    def type(self, text: str, *, snap: bool = False) -> None:
        self.type_text(text)
        time.sleep(self.settle_s)
        self._rec("type", text=text, chars=len(text))
        if snap:
            self.snap("after-type")

    def press_key(self, name: str) -> None:
        self.key(name)
        self._rec("key", key=name)

    def scroll(self, direction: str, amount: float = 0.5) -> None:
        w, h = self.screen_size()
        cx, cy = w // 2, h // 2
        d = {"down": (cx, int(cy + h * amount / 2), cx, int(cy - h * amount / 2)),
             "up": (cx, int(cy - h * amount / 2), cx, int(cy + h * amount / 2)),
             "left": (int(cx + w * amount / 2), cy, int(cx - w * amount / 2), cy),
             "right": (int(cx - w * amount / 2), cy, int(cx + w * amount / 2), cy)}[direction]
        self.swipe(*d)
        self._rec("scroll", direction=direction)

    def back(self) -> None:
        """Platform-appropriate navigation back (Android back key; iOS back button / edge swipe)."""
        self.key("back")
        self._rec("back")

    def home(self) -> None:
        self.key("home")
        self._rec("home")

    def compact_tree(self, *, type_: str | None = None, grep: str | None = None, all_: bool = False) -> str:
        lines = []
        for e in self.tree():
            if type_ and e.type.lower() != type_.lower():
                continue
            if grep and grep.lower() not in e.haystack().lower():
                continue
            if not all_ and not (e.label or e.value or e.identifier):
                continue
            lines.append(e.short())
        return "\n".join(lines)
