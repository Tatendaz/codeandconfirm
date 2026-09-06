"""The repository is public: no personal paths, personal e-mail addresses or credentials in tracked text files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "personal path": re.compile(r"/Users/[A-Za-z]|/home/[a-z][a-z0-9_-]*/"),
    "personal e-mail": re.compile(r"[A-Za-z0-9._%+-]+@(gmail|icloud|me|yahoo|outlook|hotmail)\.com"),
    "google api key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    "service account key": re.compile(r"\"private_key_id\""),
}
ALLOWED = {"tests/test_core.py", "tests/test_public_hygiene.py"}   # the tests that name the patterns themselves
ALLOWED_MATCHES = {"tatendaz@me.com"}   # the maintainer's public contact address, published on purpose


def test_tracked_files_carry_no_personal_data_or_secrets():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for rel in files:
        if rel in ALLOWED or rel.endswith((".png", ".jpg", ".gif")):
            continue
        try:
            text = (ROOT / rel).read_text(errors="strict")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for name, pat in PATTERNS.items():
            for m in pat.finditer(text):
                if m.group(0) in ALLOWED_MATCHES:
                    continue
                hits.append(f"{rel}: {name}: {m.group(0)[:40]}")
                break
    assert not hits, "\n".join(hits)
