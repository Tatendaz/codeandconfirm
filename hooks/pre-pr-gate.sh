#!/usr/bin/env bash
# CodeAndConfirm — Claude Code PreToolUse hook (matcher: Bash).
#
# Blocks `gh pr create` / `gh pr edit` / `git push` unless HEAD has a current PASS verdict recorded by
# CodeAndConfirm. It only READS the approval record (fast, no tests). It is advisory-by-design: a local
# hook can be bypassed; use the server-side status check (docs/server-side-check.md) as the merge backstop.
#
# Install (Claude Code settings.json):
#   "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
#              "command": "bash /path/to/codeandconfirm/hooks/pre-pr-gate.sh", "timeout": 20}]}]}
#
# Opt out for one command: prefix with CODEANDCONFIRM_SKIP=1 (recorded in the hook's stderr).
set -u
input="$(cat)"
cmd="$(printf '%s' "$input" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("command",""))' 2>/dev/null)"
[ -z "$cmd" ] && exit 0
case "$cmd" in
  *"gh pr create"*|*"gh pr edit"*|*"git push"*) ;;
  *) exit 0 ;;
esac
case "$cmd" in *CODEANDCONFIRM_SKIP=1*) echo "codeandconfirm: skip requested" >&2; exit 0;; esac
# Only repos that opted in.
root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$root" ] && [ -f "$root/codeandconfirm.toml" ] || exit 0
command -v codeandconfirm >/dev/null 2>&1 || { echo "codeandconfirm: CLI not on PATH; cannot verify QA verdict (allowing)" >&2; exit 0; }
if out="$(codeandconfirm gate-check --repo "$root" 2>&1)"; then
  echo "codeandconfirm: $out" >&2
  exit 0
fi
cat >&2 <<EOF
BLOCKED by CodeAndConfirm: $out
  HEAD has no current PASS verdict. Run:  codeandconfirm review --branch \$(git rev-parse --abbrev-ref HEAD) --base main
  then retry. (Every new commit needs a new verdict. Bypass once with CODEANDCONFIRM_SKIP=1 — say why to the user.)
EOF
exit 2
