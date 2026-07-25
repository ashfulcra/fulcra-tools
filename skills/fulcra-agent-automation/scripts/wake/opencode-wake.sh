#!/usr/bin/env bash
# opencode-wake — host-local wake adapter for the coord wake router.
#
# Posts ONE fixed nudge prompt to a PINNED OpenCode session on a self-hosted,
# loopback-only `opencode serve` (POST /session/<id>/prompt_async). The woken
# session's own standing orders then run wake-consume, briefing and inbox
# triage. The durable task shards on the bus are authoritative; this nudge
# carries NO work content (plan §2 content rule).
#
# There is no command surface, by construction. The invoker
# (`wake_adapters.run_script_adapter`) passes exactly three values — an agent
# id, an idempotency key and a fixed reason string — and this script has no
# flag that accepts anything executable. The prompt body is a fixed template
# built by a fixed python3 program (json.dumps); no caller-supplied byte is
# ever interpolated into shell or JSON source.
#
# CREDENTIAL RULE (load-bearing): the serve session's Basic-auth password and
# port are read AT INVOCATION TIME from a mode-600 file on the host. The
# password is NEVER embedded in this script, NEVER passed in argv (argv is
# world-readable via ps — a password there leaks to every process on the box),
# NEVER logged, and NEVER written to the store. curl receives the credential
# via a config document on stdin (`-K -`); argv carries only the loopback URL
# and the (non-secret) JSON body.
#
# Usage: opencode-wake.sh --agent <id> --key <idempotency-key> --reason <text>
# Exit:  0   nudge posted (HTTP 204), or coalesced into an already-busy session
#        1   delivery failed (server unreachable / non-204 after retries)
#        2   usage / validation / config-credential fault (nothing was posted)
#        124 adapter exceeded the local bound (only when timeout(1) exists)
#        127 curl or python3 unavailable (stripped PATH / not this host)
#        *   curl's own exit status path is mapped to 1; stderr explains
set -euo pipefail

PROG="opencode-wake"

# Config: mode-600 file with KEY=VALUE lines (no eval, strict whitelist):
#   PORT=4196                 (required; loopback port of `opencode serve`)
#   PASSWORD=...              (Basic-auth password; inline form)
#   PASSWORD_FILE=/path       (or: path to a mode-600/400 file holding it —
#                              preferred: secrets stay where they were issued)
#   SESSION_ID=ses_...        (pinned session to nudge)
#   USERNAME=opencode         (optional; default opencode)
#   AGENT_NAME=bus-runner     (optional; opencode agent addressed in the body)
#   HOST=127.0.0.1            (optional; MUST stay loopback — plaintext creds
#                              to a non-loopback host are refused)
CONFIG="${OPENCODE_WAKE_CONFIG:-${HOME:?HOME is required}/.config/opencode-wake/serve-session}"

# Local belt-and-braces bound; the executor already bounds this process from
# the outside (COORD_WAKE_ADAPTER_TIMEOUT). Skipped when no timeout(1) exists.
TIMEOUT_SECONDS="${COORD_OPENCODE_WAKE_TIMEOUT_SECONDS:-15}"
# Delivery attempts across a launchd mid-restart gap (health is re-checked per
# attempt); at-least-once delivery is safe because the nudge is content-free.
ATTEMPTS="${COORD_OPENCODE_WAKE_ATTEMPTS:-3}"

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

# Identity fields must start alphanumeric (never readable as an option) and
# stay inside the same charset the engine's invoker enforces. The reason is
# printable text, bounded. JSON-safety of all three is additionally guaranteed
# by json.dumps at body-build time.
[[ "$AGENT" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}$ ]] || die "invalid agent id"
[[ "$KEY" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$ ]] || die "invalid idempotency key"
[[ "$REASON" =~ ^[[:print:]]{1,200}$ ]] || die "invalid reason text"

command -v curl >/dev/null 2>&1 \
  || die "curl not found — this adapter requires curl" 127
command -v python3 >/dev/null 2>&1 \
  || die "python3 not found — this adapter requires python3" 127

_mode() { # Darwin/Linux file mode as octal
  case "$(uname -s)" in
    Darwin) stat -f '%Lp' "$1" 2>/dev/null ;;
    *)      stat -c '%a'  "$1" 2>/dev/null ;;
  esac
}

_require_private_file() { # $1=path $2=label
  [ -f "$1" ] || die "$2 not found: $1 (provision a mode-600 file)"
  case "$(_mode "$1")" in
    600|400) ;;
    *) die "$2 must have mode 0600 or 0400 (got $(_mode "$1")): $1" ;;
  esac
}

_require_private_file "$CONFIG" "config file"

# Parse KEY=VALUE without eval/source: strict whitelist, full-line comments and
# blanks only, no quote stripping, no metacharacter interpretation.
PORT="" PASSWORD="" PASSWORD_FILE="" SESSION_ID="" USERNAME="opencode" \
  AGENT_NAME="bus-runner" HOST="127.0.0.1"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|'#'*) continue ;;
    PORT=*)           PORT="${line#PORT=}" ;;
    PASSWORD=*)       PASSWORD="${line#PASSWORD=}" ;;
    PASSWORD_FILE=*)  PASSWORD_FILE="${line#PASSWORD_FILE=}" ;;
    SESSION_ID=*)     SESSION_ID="${line#SESSION_ID=}" ;;
    USERNAME=*)       USERNAME="${line#USERNAME=}" ;;
    AGENT_NAME=*)     AGENT_NAME="${line#AGENT_NAME=}" ;;
    HOST=*)           HOST="${line#HOST=}" ;;
    *) die "config file has an unsupported key (whitelist: PORT PASSWORD PASSWORD_FILE SESSION_ID USERNAME AGENT_NAME HOST)" ;;
  esac
done < "$CONFIG"

[ -n "$PASSWORD" ] || [ -n "$PASSWORD_FILE" ] \
  || die "config sets neither PASSWORD nor PASSWORD_FILE"
if [ -n "$PASSWORD" ] && [ -n "$PASSWORD_FILE" ]; then
  die "config sets both PASSWORD and PASSWORD_FILE — pick one (prefer PASSWORD_FILE)"
fi
if [ -n "$PASSWORD_FILE" ]; then
  _require_private_file "$PASSWORD_FILE" "password file"
  PASSWORD="$(tr -d '\r\n' < "$PASSWORD_FILE")"
fi
[ -n "$PASSWORD" ] || die "password is empty"

if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]] \
  || [ "$PORT" -lt 1 ] \
  || [ "$PORT" -gt 65535 ]; then
  die "invalid PORT"
fi
[[ "$SESSION_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}$ ]] \
  || die "invalid SESSION_ID"
[[ "$USERNAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}$ ]] || die "invalid USERNAME"
[[ "$AGENT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}$ ]] || die "invalid AGENT_NAME"
case "$HOST" in
  127.0.0.1|localhost|\[::1\]) ;;
  *) die "HOST must be loopback — refusing to send plaintext credentials off-box" ;;
esac

# Reject config-string metacharacters rather than attempting to quote a secret
# into curl's config syntax (same rule as the openclaw wake adapter).
case "$PASSWORD" in
  *$'\r'*|*$'\n'*|*'"'*|*\\*)
    die "password contains unsupported characters (CR/LF/quote/backslash)" ;;
esac

# Fixed prompt body. The template is constant; the only interpolated values are
# the charset-gated agent/key and the bounded reason, json.dumps-encoded by a
# fixed python3 program. No event prose, no commands — the nudge says only:
# check your bus.
PAYLOAD="$(python3 - "$AGENT" "$KEY" "$REASON" "$AGENT_NAME" <<'PY'
import json, sys
agent, key, reason, agent_name = sys.argv[1:]
text = (
    f"coord wake nudge for {agent} (key {key}): {reason}. Check your "
    "coordination bus now per your standing orders: consume queued wakes, run "
    "the authoritative briefing, triage your inbox, work what is addressed to "
    "you, then snapshot continuity and beat presence. This nudge encodes no "
    "work content; the durable shards on the bus are authoritative."
)
print(json.dumps({"agent": agent_name,
                  "parts": [{"type": "text", "text": text}]},
                 separators=(",", ":")))
PY
)"

BASE="http://${HOST}:${PORT}"

# curl reads the Basic credential from a config document ON STDIN (-K -): the
# password never appears in argv, never in ps, never in logs. Derived facts
# only on stdout: HTTP codes and the idempotency key.
_curl_auth() { # remaining args are curl args
  printf 'user = "%s:%s"\n' "$USERNAME" "$PASSWORD" \
    | curl --silent --show-error --max-time "$TIMEOUT_SECONDS" -K - "$@"
}

# Busy-coalesce: if the pinned session is explicitly mid-turn, the nudge is
# already satisfied — that session's standing orders triage the full inbox
# every turn, and the nudge carries no content to lose. Only an explicit
# "busy" coalesces; any doubt falls through to a real POST.
STATUS="$(_curl_auth --max-time 5 "$BASE/session/status" 2>/dev/null || true)"
if [ -n "$STATUS" ]; then
  BUSY="$(printf '%s' "$STATUS" | SID="$SESSION_ID" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
s = d.get(os.environ["SID"], {})
print(1 if isinstance(s, dict) and s.get("type") == "busy" else 0)' 2>/dev/null || echo 0)"
  if [ "$BUSY" = "1" ]; then
    echo "$PROG: session busy — nudge coalesced (the busy turn reads the full inbox); key $KEY"
    exit 0
  fi
fi

attempt=1
while [ "$attempt" -le "$ATTEMPTS" ]; do
  CODE="$(_curl_auth -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' --data-binary "$PAYLOAD" \
    "$BASE/session/$SESSION_ID/prompt_async" 2>/dev/null || echo 000)"
  if [ "$CODE" = "204" ]; then
    echo "$PROG: posted to serve session, HTTP 204; key $KEY"
    exit 0
  fi
  attempt=$((attempt + 1))
  [ "$attempt" -le "$ATTEMPTS" ] && sleep 5
done

die "delivery failed after $ATTEMPTS attempt(s) (last HTTP $CODE) — wake not delivered; key $KEY" 1
