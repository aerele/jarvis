#!/usr/bin/env bash
# review-gate.sh — PreToolUse hook for the Bash tool.
# Hard-blocks `git commit` and `gh pr create` unless the latest code AND flow
# reviews both carry VERDICT: GREEN. Skills instruct the model; this enforces.
#
# Exit 0 = allow. Exit 2 = block (stderr is shown to Claude as the reason).

set -u

INPUT="$(cat)"

# Extract the shell command from the hook payload. Prefer python3/jq; fall back
# to a raw scan so a missing parser fails safe (gate still applies).
CMD=""
if command -v python3 >/dev/null 2>&1; then
  CMD="$(printf '%s' "$INPUT" | python3 -c 'import sys,json
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("command", ""))
except Exception:
    print("")' 2>/dev/null)"
elif command -v jq >/dev/null 2>&1; then
  CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)"
else
  CMD="$INPUT"
fi

is_commit=false
is_pr=false
case "$CMD" in
  *"git commit"*) is_commit=true ;;
esac
case "$CMD" in
  *"gh pr create"*|*"glab mr create"*) is_pr=true ;;
esac

# Not a gated command — allow.
if [ "$is_commit" = false ] && [ "$is_pr" = false ]; then
  exit 0
fi

REVIEW_DIR=".claude/workflow/reviews"
CODE_FILE="$REVIEW_DIR/latest-code.md"
FLOW_FILE="$REVIEW_DIR/latest-flow.md"

green() {
  [ -f "$1" ] && grep -q "^VERDICT: GREEN" "$1"
}

missing=""
green "$CODE_FILE" || missing="code review ($CODE_FILE)"
if ! green "$FLOW_FILE"; then
  [ -n "$missing" ] && missing="$missing and "
  missing="${missing}flow review ($FLOW_FILE)"
fi

if [ -n "$missing" ]; then
  {
    echo "BLOCKED by review gate: $missing is not VERDICT: GREEN."
    echo "Run the /reviewer skill, resolve every BLOCKER and MAJOR finding,"
    echo "and re-review until both verdict files are GREEN. Commits and PRs"
    echo "are not permitted before that."
  } >&2
  exit 2
fi

exit 0
