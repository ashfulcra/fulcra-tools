#!/usr/bin/env bash
# macos-notify — host-local wake adapter for the coord wake router (W5.5).
#
# Posts ONE desktop notification telling the human at this Mac that a directed
# item is waiting on the coordination bus. It is deliberately the most
# conservative adapter there is: it DISPLAYS TEXT AND STARTS NOTHING.
#
# There is no command surface, by construction. The invoker
# (`wake_adapters.run_script_adapter`) passes exactly three values — an agent
# id, an idempotency key and a fixed reason string — and this script has no flag
# that accepts anything executable, no network call, and no interpreter. The
# notification text is handed to osascript as ARGUMENTS to a fixed
# `on run argv` program, so no caller-supplied byte is ever interpolated into
# AppleScript source.
#
# Usage: macos-notify.sh --agent <id> --key <idempotency-key> --reason <text>
# Exit:  0   notification posted
#        2   usage / validation error (nothing was posted)
#        124 osascript exceeded the local bound (only when timeout(1) exists)
#        127 osascript unavailable (not a Mac, or a stripped PATH)
#        *   osascript's own exit status
set -euo pipefail

PROG="macos-notify"

# Local belt-and-braces bound. The executor already bounds this process from the
# outside (COORD_WAKE_ADAPTER_TIMEOUT, whole process group killed); this makes
# the script safe to run by hand too. Skipped when no timeout(1) is installed —
# macOS has none in the base system.
TIMEOUT_SECONDS="${COORD_NOTIFY_TIMEOUT_SECONDS:-5}"

die() {
  echo "$PROG: $1" >&2
  exit "${2:-2}"
}

usage() {
  echo "usage: $PROG --agent <id> --key <idempotency-key> --reason <text>" >&2
}

AGENT=""
KEY=""
REASON=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --agent)  [ "$#" -ge 2 ] || die "--agent needs a value";  AGENT="$2";  shift 2 ;;
    --key)    [ "$#" -ge 2 ] || die "--key needs a value";    KEY="$2";    shift 2 ;;
    --reason) [ "$#" -ge 2 ] || die "--reason needs a value"; REASON="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown argument: $1 — this adapter accepts nothing executable" ;;
  esac
done

[ -n "$AGENT" ]  || { usage; die "--agent is required"; }
[ -n "$KEY" ]    || { usage; die "--key is required"; }
[ -n "$REASON" ] || { usage; die "--reason is required"; }

# Identity fields must start alphanumeric, so neither can be read as an option
# by osascript. The reason is printable text, bounded.
[[ "$AGENT" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}$ ]] \
  || die "invalid agent id"
[[ "$KEY" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$ ]] \
  || die "invalid idempotency key"
[[ "$REASON" =~ ^[[:print:]]{1,200}$ ]] \
  || die "invalid reason text"

command -v osascript >/dev/null 2>&1 \
  || die "osascript not found — this adapter requires macOS" 127

BOUND=()
if command -v timeout >/dev/null 2>&1; then
  BOUND=(timeout "$TIMEOUT_SECONDS")
elif command -v gtimeout >/dev/null 2>&1; then
  BOUND=(gtimeout "$TIMEOUT_SECONDS")
fi

# Fixed AppleScript program; every caller-supplied string arrives in `argv`.
# stdin is closed so nothing here can block on a read.
set +e
"${BOUND[@]:+${BOUND[@]}}" osascript \
  -e 'on run argv' \
  -e 'display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)' \
  -e 'end run' \
  "$REASON" "coord wake: $AGENT" "key $KEY" </dev/null
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
  die "osascript exited $rc — notification not posted" "$rc"
fi
exit 0
