"""Parsers for native test-result artifacts. The gate trusts these, not the worker's prose."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .util import run


@dataclass
class SuiteResult:
    kind: str
    path: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    parsed: bool = False
    detail: str = ""
    failed_tests: list[str] = field(default_factory=list)

    @property
    def green(self) -> bool:
        return self.parsed and self.total > 0 and self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["green"] = self.green
        return d


def parse_results(kind: str, path: Path) -> SuiteResult:
    path = Path(path)
    if kind == "xcresult":
        return parse_xcresult(path)
    if kind == "junit-dir":
        return parse_junit_dir(path)
    if kind == "junit":
        return parse_junit_files([path])
    if kind == "none":
        return SuiteResult(kind, str(path), parsed=True, detail="no result artifact declared; exit code only")
    return SuiteResult(kind, str(path), detail=f"unknown result kind {kind!r}")


def parse_xcresult(path: Path) -> SuiteResult:
    res = SuiteResult("xcresult", str(path))
    if not path.exists():
        res.detail = "xcresult bundle missing"
        return res
    r = run(["xcrun", "xcresulttool", "get", "test-results", "summary", "--path", str(path)], timeout=120)
    if r.ok:
        try:
            d = json.loads(r.stdout)
            res.total = int(d.get("totalTestCount", 0))
            res.passed = int(d.get("passedTests", 0))
            res.failed = int(d.get("failedTests", 0))
            res.skipped = int(d.get("skippedTests", 0))
            res.parsed = True
            res.detail = f"result={d.get('result')}"
            for f in d.get("testFailures", []) or []:
                res.failed_tests.append(f.get("testName") or f.get("testIdentifier") or str(f)[:80])
            return res
        except (json.JSONDecodeError, ValueError) as e:
            res.detail = f"summary parse error: {e}"
    # Older xcresulttool (legacy JSON)
    r = run(["xcrun", "xcresulttool", "get", "--format", "json", "--legacy", "--path", str(path)], timeout=120)
    if r.ok:
        try:
            d = json.loads(r.stdout)
            metrics = d.get("metrics", {})
            res.total = int(metrics.get("testsCount", {}).get("_value", 0))
            res.failed = int(metrics.get("testsFailedCount", {}).get("_value", 0))
            res.skipped = int(metrics.get("testsSkippedCount", {}).get("_value", 0))
            res.passed = max(0, res.total - res.failed - res.skipped)
            res.parsed = True
            res.detail = "legacy summary"
            return res
        except (json.JSONDecodeError, ValueError) as e:
            res.detail = f"legacy parse error: {e}"
    res.detail = res.detail or f"xcresulttool failed: {r.stderr.strip()[:200]}"
    return res


def parse_junit_dir(path: Path) -> SuiteResult:
    files = sorted(Path(path).rglob("*.xml")) if Path(path).exists() else []
    res = parse_junit_files(files)
    res.kind = "junit-dir"; res.path = str(path)
    if not files:
        res.detail = "no JUnit XML files found"
    return res


def parse_junit_files(files: list[Path]) -> SuiteResult:
    res = SuiteResult("junit", ", ".join(str(f) for f in files[:3]))
    any_parsed = False
    for f in files:
        try:
            raw = f.read_text(errors="replace")
            if "<!DOCTYPE" in raw or "<!ENTITY" in raw:   # test reports never need DTDs; refuse entity expansion
                continue
            root = ET.fromstring(raw)
        except (ET.ParseError, OSError):
            continue
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if not suites and root.findall("testcase"):
            suites = [root]   # node --test-reporter=junit emits <testcase> directly under <testsuites>
        for s in suites:
            any_parsed = True
            cases = list(s.iter("testcase"))
            res.total += len(cases)
            for c in cases:
                if c.find("failure") is not None:
                    res.failed += 1; res.failed_tests.append(f"{c.get('classname')}.{c.get('name')}")
                elif c.find("error") is not None:
                    res.errors += 1; res.failed_tests.append(f"{c.get('classname')}.{c.get('name')}")
                elif c.find("skipped") is not None:
                    res.skipped += 1
                else:
                    res.passed += 1
    res.parsed = any_parsed
    res.detail = f"{len(files)} file(s)"
    return res
