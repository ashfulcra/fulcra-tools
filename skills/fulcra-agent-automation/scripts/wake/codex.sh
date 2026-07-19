#!/usr/bin/env bash
# Event-driven wake adapter for an existing Codex thread. Intended as the fixed
# command passed to install-listener.sh --wake-cmd. The listener supplies team /
# agent metadata; the installer/operator supplies the exact thread id.
#
# This deliberately uses the stable, documented `codex exec resume` interface
# and preserves the resumed thread's configured approvals/sandbox. It NEVER
# adds --dangerously-bypass-approvals-and-sandbox. If the resumed turn needs an
# approval it fails/pauses according to Codex policy instead of silently gaining
# host authority.
set -euo pipefail

TEAM="${COORD_LISTENER_TEAM:?listener must set COORD_LISTENER_TEAM}"
AGENT="${COORD_LISTENER_AGENT:?listener must set COORD_LISTENER_AGENT}"
THREAD_ID="${COORD_CODEX_THREAD_ID:?set COORD_CODEX_THREAD_ID to the target Codex thread id}"
CWD="${COORD_CODEX_CWD:-}"
EVENT_REFS="${COORD_LISTENER_EVENT_REFS:-}"

[[ "$TEAM" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "codex wake: invalid team" >&2; exit 2; }
[[ "$AGENT" =~ ^[A-Za-z0-9][A-Za-z0-9:_.-]*$ ]] || { echo "codex wake: invalid agent" >&2; exit 2; }
[[ "$THREAD_ID" =~ ^[A-Za-z0-9][A-Za-z0-9:_.-]*$ ]] || { echo "codex wake: invalid thread id" >&2; exit 2; }
[[ -z "$EVENT_REFS" || "$EVENT_REFS" =~ ^[A-Z]+:[A-Za-z0-9:_.-]+(,[A-Z]+:[A-Za-z0-9:_.-]+)*$ ]] || {
  echo "codex wake: invalid event refs" >&2; exit 2; }
command -v codex >/dev/null 2>&1 || { echo "codex wake: codex CLI not found" >&2; exit 127; }

if [[ -n "$CWD" ]]; then
  [[ -d "$CWD" ]] || { echo "codex wake: COORD_CODEX_CWD is not a directory" >&2; exit 2; }
  cd "$CWD"
fi

if [[ "${COORD_LISTENER_DEGRADED:-0}" == "1" ]]; then
  REASON="listener degradation"
else
  REASON="new bus work"
fi
PROMPT="Coord event wake for ${AGENT} on team ${TEAM}: ${REASON}. Resume continuity, run the authoritative briefing once, apply documented targeted fallbacks for every degraded section, and handle all surfaced work end-to-end. Report last."
if [[ -n "$EVENT_REFS" ]]; then
  PROMPT="${PROMPT} Compact event references (kind:canonical-slug only): ${EVENT_REFS}. Use them to inspect the changed items directly; they contain no event body text."
fi

# --all allows an exact thread id to resume even when launchd's cwd differs from
# the original session. No raw bus text enters the prompt: COORD_LISTENER_OUTPUT
# is advisory/untrusted and the resumed agent fetches authoritative state itself.
exec codex exec resume --all "$THREAD_ID" "$PROMPT"
