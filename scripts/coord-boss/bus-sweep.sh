#!/bin/bash
# Direct-store sweep. Bypasses the listen/briefing folds entirely, so listener
# starvation cannot hide new work. Prints anything newer than the watermark.
SP="$(cd "$(dirname "$0")" && pwd)"
WM="$SP/.bus-sweep-watermark"
PREV=$(cat "$WM" 2>/dev/null || echo "")
CUR=$(mktemp)
{
  timeout 90 fulcra file list /team/fulcra/task/ 2>/dev/null | tr ',' '\n' \
    | grep -oE '2026-[0-9-]+ [0-9:]+[AP]M UTC  [a-z0-9-]+\.md' \
    | grep -vE '  (index|log)\.md$'   # engine-regenerated every reconcile
  timeout 60 fulcra file list /team/fulcra/_coord/agents/coord-boss/inbox/ 2>/dev/null | tr ',' '\n' \
    | grep -oE '[^ ]+\.(md|json)$' | sed 's/^/INBOX  /'
} | sort -u > "$CUR"
if [ -z "$PREV" ]; then
  echo "watermark initialised: $(wc -l < "$CUR") items known"
else
  NEW=$(comm -13 <(echo "$PREV") "$CUR")
  if [ -n "$NEW" ]; then echo "=== NEW SINCE LAST SWEEP ==="; echo "$NEW"; else echo "no new items"; fi
fi
cat "$CUR" > "$WM"; rm -f "$CUR"
