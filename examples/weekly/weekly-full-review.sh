#!/bin/bash
# Weekly full review of a branch (default: main) with the `full` profile: every journey, every suite,
# the benchmark, high effort. The base is the commit the previous weekly run tested (kept in
# $CODEANDCONFIRM_HOME/weekly/<repo-name>.last), so the diff — and the benchmark — cover "what changed
# since last week". First run: the branch's parent commit.
#
#   weekly-full-review.sh /path/to/app-repo [branch]
#
# Exit code is the verdict: 0 PASS, 1 FAIL, 3 BLOCKED, 4 CANCELLED.
set -euo pipefail
REPO="${1:?usage: weekly-full-review.sh <repo> [branch]}"
BRANCH="${2:-main}"
HOME_DIR="${CODEANDCONFIRM_HOME:-$HOME/.codeandconfirm}"
STATE_DIR="$HOME_DIR/weekly"; mkdir -p "$STATE_DIR"
LAST="$STATE_DIR/$(basename "$REPO").last"

git -C "$REPO" fetch --quiet origin "$BRANCH"
HEAD_SHA="$(git -C "$REPO" rev-parse "origin/$BRANCH")"
if [ -s "$LAST" ] && git -C "$REPO" cat-file -e "$(cat "$LAST")^{commit}" 2>/dev/null; then
  BASE="$(cat "$LAST")"
else
  BASE="$(git -C "$REPO" rev-parse "origin/$BRANCH~1")"
fi
if [ "$BASE" = "$HEAD_SHA" ]; then
  echo "weekly: $BRANCH unchanged since the last full run ($HEAD_SHA); nothing to review"
  exit 0
fi

echo "weekly: reviewing origin/$BRANCH ($HEAD_SHA) against last week's $BASE with profile full"
set +e
codeandconfirm review --repo "$REPO" --profile full --branch "origin/$BRANCH" --base "$BASE" --headless-android
status=$?
set -e
# Remember what was tested regardless of the verdict, so next week's diff starts here.
echo "$HEAD_SHA" > "$LAST"
codeandconfirm status --limit 1
exit $status
