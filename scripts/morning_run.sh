#!/bin/zsh
# Daily job-search morning run — headless Claude Code.
# Run once manually: ./scripts/morning_run.sh
# Schedule via launchd (Mac) or Task Scheduler (Windows) at your preferred time.
set -u

cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs || exit 1

CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
LOG="data/logs/morning_$(date +%F).log"

echo "=== morning run started $(date) ===" >> "$LOG"
"$CLAUDE_BIN" -p "Use the morning-run skill and execute today's run." \
  --permission-mode acceptEdits \
  --allowedTools "Bash(.venv/bin/python *),Read,Write,WebFetch,Skill" \
  >> "$LOG" 2>&1
rc=$?
echo "=== morning run finished $(date) exit=$rc ===" >> "$LOG"
