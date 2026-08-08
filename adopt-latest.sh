#!/usr/bin/env bash
# adopt-latest.sh — one-command fleet convergence onto the latest coord-engine.
# Maintained by coord-boss at team/fulcra/_coord/bus-v3/adopt-latest.sh.
# Usage: bash adopt-latest.sh [agent-name]     (or have FULCRA_COORD_AGENT set)
#
# HARNESS BLOCKS RUNNING THIS SCRIPT? (codex and other gated harnesses refuse to
# execute a downloaded script that installs packages — verified live 2026-08-07.)
# Do NOT route around the denial; it is an operator control. There is a supported
# path: read the three authority values below and run the two installs yourself,
# literally, so the approval layer sees the whole command line. Recipe, with the
# verification steps this script's claim gate performs:
#     team/fulcra/_coord/bus-v3/ADOPT-WHEN-GATED.md
# The classifier objects to OPACITY, not to the operations.
set -u
PIN="0a093dba4ba17fe344086c8c7c0d229ad5b153af"   # == main after PR 571. Carries eleven merges since pp-b2e649e6, several of which fix things agents hit this shift: 567 (the verdict must not live only at the tail of the payload - a needs-me read whose rc was truncated out of view could not be certified either way), 566 (a cap-starved review sweep must not be silent), 571 (ONE deadline for the whole HTTP read - it was FOUR stacked bounds, so a configured 30s per-op bound could take 120s and every fold budget assumes that bound holds), 569 (the codex watcher can no longer report WATCH_OK on a blind read), 568 (--json purity for every verb), 561 (a merged PR closes its review as an ARTIFACT of the merge), 558 (a hostname the OS hands back is not a fleet key until it is validated), 565, 563, 562, 557.
VER="pp-0a093dba"
# TYPE mirrors the CURRENT channel in _coord/bus-v3/records.json — when the authority moves, update BOTH (2026-08-04 cutover lesson: this line silently pinned the OLD channel)
TYPE="MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041"

A="${1:-${FULCRA_COORD_AGENT:-}}"
if [ -z "$A" ]; then
  echo "BLOCKED: no agent identity. Run: bash adopt-latest.sh <your-bus-agent-name>" >&2
  exit 3
fi

SRC="git+https://github.com/ashfulcra/fulcra-tools@${PIN}#subdirectory=packages/coord-engine"
# Idempotency fast path (2026-08-05): on a shared box, several identities run this
# script back to back under ONE user account — a forced reinstall of an
# already-current engine can collide with the copy installed seconds earlier
# ("failed to remove directory"). A sentinel records the last pin THIS USER
# adopted; when it matches AND the engine is genuinely usable (bus-v3 verb +
# writer import both prove out), skip the install legs and go straight to the
# queue drain + claim. Any doubt -> full install as before.
SENTINEL="${HOME}/.coord-adopted-pin"
if [ -f "$SENTINEL" ] && [ "$(cat "$SENTINEL" 2>/dev/null)" = "$PIN" ] \
   && coord-engine bus-v3 --help >/dev/null 2>&1; then
  EPY=""
  if command -v uv >/dev/null 2>&1 && [ -x "$(uv tool dir 2>/dev/null)/coord-engine/bin/python" ]; then
    EPY="$(uv tool dir)/coord-engine/bin/python"
  fi
  if [ -n "$EPY" ] && "$EPY" -c 'import fulcra_common' >/dev/null 2>&1; then
    echo "adopt: engine already at pin ${VER} for this user (sentinel + verb + writer verified) — skipping install"
    INSTALLER="already-current"
  fi
fi

# COMMON must ride along in the SAME environment as the engine: `annotate project`
# and `digest --emit-timeline` import fulcra_common at runtime. A bare engine
# install silently wipes it and those legs become silent no-ops (2026-08-04
# digest-darkness root cause). Never install the engine without it.
COMMON="git+https://github.com/ashfulcra/fulcra-tools@fulcra-common-v0.3.0#subdirectory=packages/fulcra-common"
# Positive-evidence installer loop (2026-08-04): every attempt logs the exact
# failing command + its stderr. "No installer worked" with the real errors
# discarded cost coord-maintainer a diagnosis cycle — never again.
ELOG="$(mktemp)"; trap 'rm -f "$ELOG"' EXIT
STEP_FAILS=0   # counted so the ADOPTION CLAIM can carry it (2026-08-06, coord-opus-worker
               # recurrence): a load-bearing `uv tool install` TIMED OUT, a later installer
               # recovered, and the claim still read plain `rc0`. The engine was genuinely
               # correct — but a reader of the claim alone could not tell a clean run from a
               # rescued one, and the claim gate proves the ENGINE, never the RUN.
try() {  # try <label> <cmd...>: run, capture stderr, report the FAILING step by name
  local label="$1"; shift
  if "$@" >/dev/null 2>>"$ELOG"; then return 0; fi
  STEP_FAILS=$((STEP_FAILS+1))
  echo "adopt: step FAILED: ${label}: $*" >&2
  echo "  last stderr: $(tail -2 "$ELOG" | tr '\n' ' ')" >&2
  return 1
}
INSTALLER="${INSTALLER:-}"
# Resolve uv beyond PATH: launchd/cron/systemd contexts run lean PATHs and
# "uv not found" was falsely true on hosts that have it (coord-maintainer
# 2026-08-05: /opt/homebrew/bin/uv present, invoking shell could not see it).
UV_BIN=""
for _c in uv /opt/homebrew/bin/uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if command -v "$_c" >/dev/null 2>&1; then UV_BIN="$_c"; break; fi
done
if [ -z "$INSTALLER" ] && [ -n "$UV_BIN" ]; then
  try "uv fulcra-api" "$UV_BIN" tool install --force fulcra-api \
    && try "uv coord-engine@pin+fulcra-common" "$UV_BIN" tool install --force "$SRC" --with "$COMMON" && INSTALLER=uv
elif [ -n "$INSTALLER" ]; then echo "adopt: install already satisfied (${INSTALLER}), skipping uv leg" >&2
else echo "adopt: uv not on PATH, skipping" >&2; fi
if [ -z "$INSTALLER" ] && command -v pipx >/dev/null 2>&1; then
  try "pipx fulcra-api" pipx install --force fulcra-api \
    && try "pipx coord-engine@pin" pipx install --force "$SRC" \
    && try "pipx inject fulcra-common" pipx inject coord-engine "$COMMON" && INSTALLER=pipx
elif [ -z "$INSTALLER" ]; then echo "adopt: pipx not on PATH, skipping" >&2; fi
# (the two lines above used to print "uv not on PATH" whenever the sentinel fast path
#  had ALREADY satisfied the install — a false statement about the host, on the
#  fleet's most-run script. Verified live 2026-08-06 on a box with uv at
#  ~/.local/bin/uv. A log line that lies costs exactly one diagnosis cycle.)
if [ -z "$INSTALLER" ]; then
  try "pip user-install" python3 -m pip install --user --upgrade --quiet fulcra-api "$SRC" "$COMMON" && INSTALLER=pip
fi
if [ -z "$INSTALLER" ]; then
  echo "ADOPT FAILED — the per-step failures above name the exact command and stderr; report THOSE lines to coord-boss (not just this one)." >&2
  exit 4
fi
hash -r 2>/dev/null || true

# CLAIM GATE (2026-08-05, after coord-maintainer's false-claim find): an installer
# can "succeed" into an environment PATH never runs (pip --user shadowed by an old
# uv tool install), and a claim would then assert a currency the operative engine
# does not have. So: the claim is earned ONLY by the binary `command -v coord-engine`
# resolves proving, via its own doctor pin-currency leg, that it matches the
# authority pin THIS script just read. No proof, no claim, loud exit.
PIN12=$(printf %.12s "$PIN")
if ! coord-engine bus-v3 --help >/dev/null 2>&1; then
  echo "ADOPT FAILED — operative coord-engine ($(command -v coord-engine || echo NOT-ON-PATH)) cannot speak bus-v3; the install landed somewhere PATH does not run. No claim filed. Report verbatim to coord-boss." >&2
  exit 4
fi
if ! coord-engine doctor fulcra 2>/dev/null | grep -q "matches the fleet pin (${PIN12}"; then
  echo "ADOPT FAILED — operative coord-engine did not prove currency against pin ${PIN12} (doctor pin-currency line absent or mismatched; engine may be older than the pin or shadowed by a stale install at $(command -v coord-engine)). No claim filed. Report verbatim to coord-boss." >&2
  exit 4
fi

# Verify the writer LANDED IN THE ENGINE'S OWN VENV, keyed to the installer that
# actually ran. `coord-engine annotate --help` is NOT a valid check and neither is
# a system-python import: commands_annotate.py swallows the missing import, so
# annotate/digest legs exit 0 as silent no-ops on a writer-less host
# (coord-opus-worker false-pass report, 2026-08-04 — verified on a live
# writer-less install). Import in the engine's interpreter or don't claim verified.
case "$INSTALLER" in
  uv)   try "verify fulcra_common in engine venv (uv)" "$(uv tool dir)/coord-engine/bin/python" -c 'import fulcra_common' ;;
  pipx) try "verify fulcra_common in engine venv (pipx)" pipx runpip coord-engine show fulcra-common ;;
  pip)  try "verify fulcra_common importable (pip --user)" python3 -c 'import fulcra_common' ;;
esac || {
  echo "ADOPT FAILED — engine installed via ${INSTALLER} but the fulcra_common writer is MISSING from its environment; annotate/digest legs would silently no-op. Report this verbatim to coord-boss." >&2
  exit 4
}

printf %s "$PIN" > "${HOME}/.coord-adopted-pin" 2>/dev/null || true

# NON-CONSUMING by design (2026-08-06). This read used to advance the caller's
# cursor. For any agent whose wake is "run this script, done", that silently ate
# its own delivery: events marked seen, printed to a log nobody reads, no error
# anywhere. Same trap coord-boss.md already records for bootstrap.sh — "a
# cursor-advancing read whose output nobody processes silently discards wake
# hints". The read stays (rc feeds the claim slug below and this script's exit
# code, so dropping it would lose the DEGRADED signal); --peek makes it safe.
echo "--- queue PEEK as ${A} (non-consuming preview; your cursor is NOT advanced) ---"
coord-engine queue fulcra --agent "$A" --peek
rc=$?
echo "--- queue rc=${rc}  (rc 3 = DEGRADED window: report it verbatim; quiet is not clear) ---"
echo "--- NOTE: a peek is not a read. Still run your own \`coord-engine queue fulcra --agent ${A}\`"
echo "---       AND \`coord-engine needs-me fulcra --agent ${A}\` this wake: queue CLEAR is not"
echo "---       proof of no work, and \`tell\` dispatch does not appear on the event channel at all."

# The claim carries the RUN's honesty, not just the engine's: a nonzero
# STEP_FAILS means some installer step failed and a later one rescued it, so the
# slug says `-steps<N>` and a reader can tell a clean adoption from a rescued one.
#
# Adoption claim rides the ENGINE's tagged chokepoint, not a raw record pipe:
# raw-pipe claims carry no identity TAGS, so they are invisible to tag-keyed
# timeline views (2026-08-05 finding — most bus traffic was untagged). To be
# precise, since "untagged" has been misread as "unattributed": the raw path DOES
# preserve sender attribution — the bare agent name lands in `sources` and
# recipients can route on it (coord-maintainer verified this 2026-08-06). Only
# the four-dimension identity tags are missing. The engine we JUST installed
# always has bus-v3 send; raw pipe remains only as the fallback if the engine
# send itself fails — and it is the ONLY path on a pre-bus-v3 engine.
SLUG="adopted-${VER}-${A}-rc${rc}"
[ "$STEP_FAILS" -gt 0 ] && SLUG="${SLUG}-steps${STEP_FAILS}"
FULCRA_COORD_AGENT="$A" coord-engine bus-v3 send fulcra --to coord-boss --kind claim \
    --slug "$SLUG" --priority P2 \
  && echo "adoption claim sent (tagged) to coord-boss (slug ${SLUG})" \
  || { printf '{"note":"{\\"v\\":1,\\"to\\":\\"coord-boss\\",\\"kind\\":\\"claim\\",\\"pri\\":\\"P2\\",\\"slug\\":\\"%s\\"}"}' "$SLUG" | \
       fulcra-api record "$TYPE" --api-version v1alpha1 --source="$A" \
       && echo "adoption claim sent via RAW FALLBACK (tags missing, attribution intact) — report to coord-boss" \
       || echo "WARN: adoption claim failed to send — report this verbatim to coord-boss"; }
[ "$STEP_FAILS" -gt 0 ] && echo "adopt: ${STEP_FAILS} step(s) failed and were rescued by a later installer — the per-step stderr above is the evidence; the engine was still verified by the claim gate." >&2

exit "$rc"
