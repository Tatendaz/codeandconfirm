#!/usr/bin/env python3
"""Copy a run's report into docs/sample-report.md with local paths and identifiers generalized."""
import os, re, sys, pathlib

if len(sys.argv) != 2:
    sys.exit("usage: export-sample-report.py <run-id>")
home = pathlib.Path(os.environ.get("CODEANDCONFIRM_HOME") or "~/.codeandconfirm").expanduser()
src = home / "runs" / sys.argv[1] / "report.md"
text = src.read_text()
text = text.replace(str(home), "$CODEANDCONFIRM_HOME").replace(str(pathlib.Path.home()), "~")
text = re.sub(r"github\.com/[^/\s)]+/[^/\s)]+", "github.com/OWNER/REPO", text)
text = re.sub(r"\[[^\]]+#(\d+)\]", r"[OWNER/REPO#\1]", text)
text = re.sub(r"by [A-Za-z0-9_-]+ ·", "by AUTHOR ·", text)
out = pathlib.Path(__file__).resolve().parents[1] / "docs" / "sample-report.md"
out.write_text("# Sample report\n\nA real report from the first integration target, with local paths and account names generalized.\n\n---\n\n" + text)
print(out)
