#!/usr/bin/env bash
# Demonstration: cancellation cleans up owned resources.
#
# Starts a review in the background, waits until it is building (or later), cancels it, and prints
# what is left behind: coordinator/worker/suite processes, backend ports, reservations, run verdict.
#
#   scripts/demo-cancel.sh --repo /path/to/app --config /path/to/codeandconfirm.toml --branch feat/x --base main
set -euo pipefail
LOG="$(mktemp -t cac-demo-cancel.XXXXXX)"
codeandconfirm review "$@" >"$LOG" 2>&1 &
CPID=$!
echo "coordinator pid $CPID, log $LOG"
RID=""
for _ in $(seq 1 120); do
  RID=$(grep -oE 'run [0-9]{8}-[0-9]{6}-[0-9a-f]{6}' "$LOG" | head -1 | awk '{print $2}' || true)
  [ -n "$RID" ] && break
  sleep 1
done
[ -n "$RID" ] || { echo "no run id found"; cat "$LOG"; exit 1; }
echo "run id $RID — waiting for the build phase"
until grep -qE 'building…|suite .*running|worker .*started' "$LOG"; do
  if ! kill -0 "$CPID" 2>/dev/null; then echo "coordinator exited early:"; tail -20 "$LOG"; exit 1; fi
  sleep 2
done
sleep 5
echo "--- cancelling"
codeandconfirm cancel "$RID"
sleep 3
echo "--- leftovers (expect none)"
echo "coordinator alive: $(kill -0 "$CPID" 2>/dev/null && echo yes || echo no)"
echo "codex workers:    $(pgrep -fl 'codex exec' | wc -l | tr -d ' ')"
echo "xcodebuild/gradle for this run: $(pgrep -fl "$RID" | grep -vE 'demo-cancel|grep' | wc -l | tr -d ' ')"
for p in 9099 8080 9199 5001; do lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1 && echo "port $p STILL LISTENING" || true; done
echo "reservations held by $RID: $(codeandconfirm locks | grep -c "$RID" || true)"
codeandconfirm status "$RID" | head -3
