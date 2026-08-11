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
#
# ============================================================================
# A FAILED ADOPT MUST NEVER STRIP A CAPABILITY THE HOST HAD AT START.
# ============================================================================
# This is the invariant every leg below is written to hold, and the one to check
# any NEW leg against (coord-boss ruling, 2026-08-10). Convergence is worth a
# failed run; it is never worth a host that can no longer reach the bus.
#
# CAPABILITY is the operative word, and it is narrower than "file". Something
# that cannot run is not a capability: nothing on the host can use it, so
# replacing it takes nothing away and is what lets a wrecked install recover
# without a human. What the invariant forbids is destroying something that
# WORKS, or something whose state you cannot classify.
#
# It has been broken once, exactly as stated: `uv tool install --force
# fulcra-api` deletes the tool environment before reinstalling, the delete failed
# with "Directory not empty (os error 66)" AFTER bin/ was already gone, and the
# host was left with no store client at all — off the bus, with `doctor` blaming
# the store for an unreadable adoption authority. Concretely, then: probe before
# you replace; do not mutate a working capability at all in an unattended run
# unless the failure can be rolled back, and this store offers nothing to roll
# back to; treat "the probe failed" as UNKNOWN rather than as "nothing is
# installed"; and when you cannot repair something, leave it alone and say so
# loudly instead of deleting it.
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
# adopted; when it matches AND the engine on disk IS that pin, skip the install
# legs and go straight to the queue drain + claim. Any doubt -> full install.
#
# THE SENTINEL IS A CLAIM, NOT EVIDENCE (2026-08-11). It records a past action of
# this user, not the state of the engine now. Paired only with `bus-v3 --help` it
# proved "I once adopted X" and "some bus-v3 engine is installed" — and since
# EVERY pin since bus-v3 shipped carries that verb, verb-presence cannot tell pin
# X from pin Y. So the pair could not distinguish the one case that matters.
# That state is reachable: coord-opus-worker's box restores a disk snapshot
# frequently-but-not-always and NOT UNIFORMLY — a wake came up with $HOME intact
# while /tmp was empty. The sentinel lives in $HOME; the engine lives in the uv
# tool dir. Once those revert independently, "my marker survived" stops implying
# "the engine it names survived". That box fails safe today only because its
# image predates bus-v3; a snapshot taken now would pass the verb check.
# So compare the BUILD: `direct_url.json`'s `vcs_info.commit_id`, read from the
# engine's own environment — the build-identity mechanism this repo settled on in
# PR 598. Sentinel and pin are both full 40-hex commits, so it is a direct
# comparison. Anything unreadable, malformed, or non-VCS is UNKNOWN, and UNKNOWN
# falls through to the full install: a skipped install on a stale engine is
# silent and lasts the whole wake, while a redundant one costs ~30-60s.
SENTINEL="${HOME}/.coord-adopted-pin"
engine_is_current() {
  [ -f "$SENTINEL" ] && [ "$(cat "$SENTINEL" 2>/dev/null)" = "$PIN" ] || return 1
  coord-engine bus-v3 --help >/dev/null 2>&1 || return 1
  EPY=""
  if command -v uv >/dev/null 2>&1 && [ -x "$(uv tool dir 2>/dev/null)/coord-engine/bin/python" ]; then
    EPY="$(uv tool dir)/coord-engine/bin/python"
  fi
  [ -n "$EPY" ] || return 1
  "$EPY" -c 'import fulcra_common' >/dev/null 2>&1 || return 1
  # Read the commit the engine was actually built from. Printing nothing on any
  # failure keeps every error path on the UNKNOWN side of the comparison.
  # The env root is DERIVED FROM EPY rather than from interpreter introspection:
  # `sys.executable` reports whatever the running interpreter resolves to, which
  # is not this environment when python is reached through a wrapper. We already
  # know which environment we are asking about — ask about that one.
  BUILT="$("$EPY" - "${EPY%/bin/python}" <<'PYEOF' 2>/dev/null
import glob, json, os, sys
root = sys.argv[1]
for pat in ("coord_engine-*.dist-info", "coord-engine-*.dist-info"):
    for d in glob.glob(os.path.join(root, "lib", "*", "site-packages", pat)):
        try:
            with open(os.path.join(d, "direct_url.json")) as fh:
                c = json.load(fh).get("vcs_info", {}).get("commit_id")
        except Exception:
            continue
        if c:
            print(c)
            sys.exit(0)
PYEOF
)" || BUILT=""
  [ -n "$BUILT" ] && [ "$BUILT" = "$PIN" ] || return 1
  return 0
}
if engine_is_current; then
  echo "adopt: engine already at pin ${VER} for this user (sentinel + verb + writer + BUILD COMMIT verified) — skipping install"
  INSTALLER="already-current"
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
# fulcra-api is the STORE CLIENT: every read and write on this bus goes through
# it, so losing it takes the host OFF THE BUS entirely — and `doctor` then reports
# the adoption authority unreadable, which points a diagnosis at the store instead
# of at us. `uv tool install --force` DELETES the tool environment before
# reinstalling, and on macOS that delete can fail with "Directory not empty (os
# error 66)" AFTER bin/ is already gone, leaving a dangling shim, a directory uv
# itself calls malformed, and no executable. Measured on this fleet 2026-08-10
# (coord-maintainer, macOS + uv 0.11.17), running this script as a pin's own
# acceptance test: it destroyed a WORKING client and every fallback leg then
# failed for unrelated host reasons.
#
# So: a client that WORKS is left alone entirely. Force-install only when there
# is nothing to lose, and if that fails, clear the half-removed directory and
# retry once instead of cascading. A client that is present but broken gets one
# repair attempt, whose result is RE-PROBED rather than believed.
#
# THE GENERAL RULE, which any future leg here must also honour: a failed adopt
# must never leave the host worse than it found it.
# CLASSIFIED ONCE, BEFORE ANY INSTALLER RUNS, because the decision belongs to the
# whole cascade and not to one leg. The uv helper protected the client and the
# pipx and pip fallbacks still carried `fulcra-api` in their package lists, so an
# unrelated ENGINE install failure dropped through and force-reinstalled the very
# client the helper had just promised not to touch (codex-reviewer, 597 r5). A
# rule that lives in one leg is a rule the other legs silently lack — the third
# time in this PR, and the reason it is a variable now rather than a habit.
#
#   working — leave it ALONE everywhere. No leg may install, upgrade or replace it.
#   broken  — present and executable but not answering. Repair non-destructively
#             or not at all; no leg may force-install over it.
#   absent  — nothing on PATH, a shim whose target is gone, or a file that cannot
#             be executed. Not a capability, so any leg may install it freely.
client_state() {
  if fulcra-api --help >/dev/null 2>&1; then echo working; return; fi
  _FA="$(command -v fulcra-api 2>/dev/null || true)"
  if [ -n "$_FA" ] && [ -x "$_FA" ]; then echo broken; return; fi
  echo absent
}
CLIENT_STATE="$(client_state)"

uv_store_client() {
  if [ "$CLIENT_STATE" = working ]; then
    # WORKING. Do not touch it at all.
    #
    # I previously upgraded it here and called that non-destructive. That was an
    # ASSUMPTION, not uv's documented contract — `uv tool upgrade` reinstalls a
    # tool's executables even when they have not changed, so a failed upgrade
    # can leave the tool broken exactly the way a failed `--force` install can,
    # and the branch then returned 0 without re-probing (codex-reviewer, 597
    # r3). Same unchecked partial mutation as the bug this PR exists to fix, in
    # the branch written to prevent it.
    #
    # Re-probing alone would only stop the FALSE SUCCESS; the host would still
    # have lost the capability. This store has no restorable snapshot of a tool
    # environment that failure could roll back to, so the invariant in the
    # header leaves one honest option: don't mutate the thing the bus depends on
    # in an unattended script. Adoption exists to converge the ENGINE onto the
    # pin; upgrading the client is a nice-to-have, and it is not worth the one
    # capability we cannot lose.
    #
    # Says so out loud rather than silently, so a host stranded on an old client
    # is visible and a human can upgrade it deliberately, watching.
    echo "adopt: fulcra-api present and working — left untouched (an unattended upgrade cannot be rolled back if it breaks; run 'uv tool upgrade fulcra-api' yourself when you can watch it)" >&2
    return 0
  fi
  # NOT WORKING IS TWO DIFFERENT FACTS, and only one of them is safe to act on.
  # A forced reinstall DELETES what is there, so treating "the probe failed" as
  # "nothing is installed" is how a diagnostic wrecks a working host
  # (coord-opus-worker, 597 r1).
  #
  # ABSENT, precisely, is any of: nothing on PATH; a shim whose target is gone;
  # or a file that CANNOT BE EXECUTED. None of those is a capability — nothing
  # on this host can use them — so replacing them costs nothing and is what
  # recovers a wrecked install without a human. The header invariant protects
  # CAPABILITIES, not the files left behind when one dies.
  #
  # Anything else — present, executable, and still not answering — is UNKNOWN
  # and must not be destroyed to satisfy this script.
  #
  # That distinction is stated because the earlier wording said "anything else
  # is UNKNOWN", which read as forbidding exactly what the next line does to a
  # non-executable file (coord-opus-worker, 597 r4, traced through the guard
  # rather than argued). On the one path here that deletes, prose disagreeing
  # with code is how the next reader gets it wrong; the behaviour is deliberate,
  # so the comment now says so.
  _FA="$(command -v fulcra-api 2>/dev/null || true)"
  if [ "$CLIENT_STATE" = broken ]; then
    # Present, executable, and still would not run. Repair NON-destructively or
    # not at all: an upgrade can fix a broken environment without deleting it
    # first, and if it cannot, a human should see why before anything is removed.
    echo "adopt: fulcra-api is present at ${_FA} but did not run — NOT force-reinstalling, because that deletes it first. Trying an in-place repair." >&2
    if try "uv fulcra-api (repair in place)" "$UV_BIN" tool upgrade fulcra-api \
       && fulcra-api --help >/dev/null 2>&1; then
      return 0
    fi
    echo "adopt: fulcra-api at ${_FA} is present but broken, and an in-place repair did not fix it. Refusing to delete it — look at WHY it fails, then, if you still want a clean reinstall: uv tool uninstall fulcra-api; rm -rf \"\$(uv tool dir)/fulcra-api\"; uv tool install --force fulcra-api" >&2
    return 1
  fi
  # ABSENT per the definition above — nothing on PATH, a shim whose target is
  # gone (exactly the wreckage the ENOTEMPTY failure leaves), or a file that
  # cannot be executed. Nothing to lose, so the forced install is correct, and
  # it is what lets a host recover on its own.
  if try "uv fulcra-api" "$UV_BIN" tool install --force fulcra-api; then return 0; fi
  # Self-heal the half-removed state. `uv tool uninstall` alone does not clear it
  # (the directory is exactly what failed to delete), and the retry REQUIRES
  # --force: fulcra-api ships two entry points, and the one that survives the
  # wreck makes a plain install fail with "Executable already exists: fulcra".
  echo "adopt: fulcra-api install failed — clearing a half-removed tool dir and retrying once" >&2
  "$UV_BIN" tool uninstall fulcra-api >/dev/null 2>&1 || true
  _TOOLDIR="$("$UV_BIN" tool dir 2>/dev/null)"
  [ -n "$_TOOLDIR" ] && rm -rf "${_TOOLDIR}/fulcra-api" 2>/dev/null
  try "uv fulcra-api (retry after clearing the tool dir)" "$UV_BIN" tool install --force fulcra-api
}
if [ -z "$INSTALLER" ] && [ -n "$UV_BIN" ]; then
  uv_store_client \
    && try "uv coord-engine@pin+fulcra-common" "$UV_BIN" tool install --force "$SRC" --with "$COMMON" && INSTALLER=uv
elif [ -n "$INSTALLER" ]; then echo "adopt: install already satisfied (${INSTALLER}), skipping uv leg" >&2
else echo "adopt: uv not on PATH, skipping" >&2; fi
if [ -z "$INSTALLER" ] && command -v pipx >/dev/null 2>&1; then
  # Only when ABSENT. A working or broken client is never replaced by a leg that
  # exists to install the ENGINE (597 r5).
  { [ "$CLIENT_STATE" != absent ] \
      || try "pipx fulcra-api" pipx install --force fulcra-api; } \
    && try "pipx coord-engine@pin" pipx install --force "$SRC" \
    && try "pipx inject fulcra-common" pipx inject coord-engine "$COMMON" && INSTALLER=pipx
elif [ -z "$INSTALLER" ]; then echo "adopt: pipx not on PATH, skipping" >&2; fi
# (the two lines above used to print "uv not on PATH" whenever the sentinel fast path
#  had ALREADY satisfied the install — a false statement about the host, on the
#  fleet's most-run script. Verified live 2026-08-06 on a box with uv at
#  ~/.local/bin/uv. A log line that lies costs exactly one diagnosis cycle.)
if [ -z "$INSTALLER" ]; then
  # Same rule: `--upgrade fulcra-api` would MUTATE a working client, which is
  # exactly what r3 established this script must not do unattended.
  if [ "$CLIENT_STATE" = absent ]; then
    try "pip user-install" python3 -m pip install --user --upgrade --quiet fulcra-api "$SRC" "$COMMON" && INSTALLER=pip
  else
    try "pip user-install (engine only)" python3 -m pip install --user --upgrade --quiet "$SRC" "$COMMON" && INSTALLER=pip
  fi
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

# CLAIM DEDUPE, HELD IN THE STORE (2026-08-08). This send fired on EVERY
# invocation, so an agent on an hourly wake re-announced the same adoption every
# hour — five identical claims in one afternoon, pure queue noise for every
# recipient.
#
# WHY NOT THE ${HOME} SENTINEL ABOVE: it gates the INSTALL legs and cannot gate
# this. A container rebuilt from a snapshot each wake restores $HOME, so the
# sentinel never matches there — and those hosts are exactly the ones re-running
# this script hourly. A dedupe held on local disk is a dedupe for the hosts that
# were already quiet. The fact has to live where the identity lives.
#
# KEYED ON THE FULL SLUG, not on (agent, pin): the slug encodes rc and
# `-steps<N>`, so a RESCUED run still announces itself. Deduping on (agent,
# pin) would suppress exactly the signal the -steps suffix exists to carry.
# NOTE the limit of the key (codex-reviewer, 589 r1): `-steps<N>` counts how
# many steps were rescued, not WHICH, so equal slugs mean equal rc and equal
# step COUNT — not provably the same outcome. The dedupe is right for noise
# suppression and must not be read as an identity claim.
#
# FAILS OPEN: if the marker cannot be read we SEND. A duplicate claim is noise;
# a missed one is a fleet that cannot tell who adopted. An unreadable store must
# never become a silent skip.
CLAIM_MARK="team/fulcra/_coord/bus-v3/adopted/${SLUG}.txt"
if fulcra-api file stat "$CLAIM_MARK" >/dev/null 2>&1; then
  echo "adopt: ${A} already claimed ${VER} with this outcome — not re-sending (store marker)"
else
SENT=0
if FULCRA_COORD_AGENT="$A" coord-engine bus-v3 send fulcra --to coord-boss --kind claim \
     --slug "$SLUG" --priority P2; then
  SENT=1
  echo "adoption claim sent (tagged) to coord-boss (slug ${SLUG})"
elif printf '{"note":"{\\"v\\":1,\\"to\\":\\"coord-boss\\",\\"kind\\":\\"claim\\",\\"pri\\":\\"P2\\",\\"slug\\":\\"%s\\"}"}' "$SLUG" | \
       fulcra-api record "$TYPE" --api-version v1alpha1 --source="$A"; then
  SENT=1
  echo "adoption claim sent via RAW FALLBACK (tags missing, attribution intact) — report to coord-boss"
else
  echo "WARN: adoption claim failed to send — report this verbatim to coord-boss"
fi
  # Marker written ONLY on a delivery that actually succeeded (codex-reviewer,
  # 589 r1). The first cut wrote it after the send BLOCK regardless: the final
  # warning `echo` succeeds, so control reached the upload and a claim that was
  # never delivered still suppressed the next wake's retry — the exact opposite
  # of the fail-open contract three comments up, and a false-clear on the
  # adoption record itself.
  #
  # Best-effort in the other direction only: a failed marker WRITE costs one
  # duplicate claim next wake, which is the safe way to be wrong.
  #
  # The cleanup lives INSIDE the branch because `_CM` is assigned inside it and
  # this script runs under `set -u` (line 14). Left outside, `rm -f "$_CM"`
  # expands an unset variable on the SENT=0 path and the shell EXITS there:
  # `exit "$rc"` below is never reached, so a DEGRADED rc 3 is replaced by a
  # generic 1 and the rescued-step evidence line never prints. It fires only
  # when the send already failed — i.e. when the bus is degraded — so a bus
  # outage would read as a broken adopt script (coord-opus-worker, 589 r2).
  # `${_CM:-}` silences it too; scoping states structurally which paths reach
  # the line.
  if [ "$SENT" -eq 1 ]; then
    _CM="$(mktemp)"; printf 'claimed %s\n' "$SLUG" > "$_CM"
    fulcra-api file upload "$_CM" "$CLAIM_MARK" >/dev/null 2>&1 \
      || echo "adopt: claim marker not written — the claim may repeat next wake" >&2
    rm -f "$_CM"
  fi
fi
[ "$STEP_FAILS" -gt 0 ] && echo "adopt: ${STEP_FAILS} step(s) failed and were rescued by a later installer — the per-step stderr above is the evidence; the engine was still verified by the claim gate." >&2

exit "$rc"
